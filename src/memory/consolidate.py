"""Maintain memory through session promotion, decay, and retention.

Promote durable idle-session task state after conflict resolution and archive its copy.
Archive low-scoring memories for recovery, then delete inactive records beyond retention.
"""

from __future__ import annotations

import math

from src.config import settings
from src.memory.models import MemoryCandidate, MemoryScope
from src.memory.service import MemoryService
from src.midplatform.ops_store import now

DECAY_FLOOR = 0.05


def decay_score(importance: float, last_seen: float, access_count: int, at: float) -> float:
    """Compute a Generative-Agents-style retention score in [0, 1]."""
    hours = max(at - last_seen, 0) / 3600
    recency = math.pow(0.995, hours)
    usage = 1 - math.exp(-0.5 * (access_count + 1))
    return recency * (importance / 10) * usage


async def promote_idle_sessions(service: MemoryService, at: float | None = None) -> int:
    """Promote idle-session task state to tenant-level facts."""
    at = at if at is not None else now()
    cutoff = at - settings.MEMORY_SESSION_IDLE_HOURS * 3600
    ops = service.store.ops
    rows = ops.query(
        "SELECT * FROM memories WHERE scope = 'session' AND status = 'active' "
        "AND COALESCE(last_accessed, created_at) < ?",
        (cutoff,),
    )
    promoted = 0
    for row in rows:
        if row["importance"] >= 6:
            candidate = MemoryCandidate(
                scope="tenant",
                kind="fact",
                subject=row["subject"],
                attribute=row["attribute"],
                value_text=row["value_text"],
                confidence=row["confidence"],
                source=row["source"],
                importance=row["importance"],
                evidence_ref=row["evidence_ref"],
            )
            scope = MemoryScope(user_id=row["user_id"], tenant_id=row["tenant_id"], chat_id=None)
            if await service.write(candidate, scope, at=at):
                promoted += 1
        ops.execute(
            "UPDATE memories SET status = 'archived', valid_to = ? WHERE id = ?", (at, row["id"])
        )
    return promoted


def decay_and_retain(service: MemoryService, at: float | None = None) -> dict[str, int]:
    """Archive decayed memories and delete inactive records beyond retention."""
    at = at if at is not None else now()
    ops = service.store.ops
    archived = 0
    for row in ops.query(
        "SELECT * FROM memories WHERE status = 'active' AND source != 'user_explicit'"
    ):
        score = decay_score(
            row["importance"], row["last_accessed"] or row["created_at"], row["access_count"], at
        )
        if score < DECAY_FLOOR:
            ops.execute("UPDATE memories SET status = 'archived' WHERE id = ?", (row["id"],))
            archived += 1
    cutoff = at - settings.MEMORY_RETENTION_DAYS * 86_400
    stale = ops.query(
        "SELECT id FROM memories WHERE status IN ('superseded','archived') AND created_at < ?",
        (cutoff,),
    )
    if stale:
        ids = [row["id"] for row in stale]
        marks = ",".join("?" for _ in ids)
        ops.execute(f"DELETE FROM memories WHERE id IN ({marks})", tuple(ids))
        service.store.index.delete(ids)
    return {"archived": archived, "purged": len(stale)}
