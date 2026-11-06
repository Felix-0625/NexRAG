"""后台记忆维护：会话提升、衰减、保留期清理。

- 空闲会话：持久的 task_state 结论经常规冲突消解后提升到 tenant 层，
  会话内的副本归档。
- 衰减：时效 x 重要性 x 使用频次低于阈值的记忆会被归档（状态 "archived"），
  不做物理删除，因此可以恢复。
- 保留期：超过 MEMORY_RETENTION_DAYS 且非 active 的记录会被清除。
"""

from __future__ import annotations

import math

from src.config import settings
from src.memory.models import MemoryCandidate, MemoryScope
from src.memory.service import MemoryService
from src.midplatform.ops_store import now

DECAY_FLOOR = 0.05


def decay_score(importance: float, last_seen: float, access_count: int, at: float) -> float:
    """Generative-Agents 风格的留存分数，取值 [0, 1]。"""
    hours = max(at - last_seen, 0) / 3600
    recency = math.pow(0.995, hours)
    usage = 1 - math.exp(-0.5 * (access_count + 1))
    return recency * (importance / 10) * usage


async def promote_idle_sessions(service: MemoryService, at: float | None = None) -> int:
    """将空闲对话的 task_state 记忆提升为 tenant 级事实。"""
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
    """归档已衰减的记忆，并清除超过保留期的非活跃记录。"""
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
