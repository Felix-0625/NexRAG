"""Authoritative operations-store memory records and their vector index.

Commit SQLite first; queue failed vector writes for replay. Recall always rechecks state
and visibility against SQLite, ensuring eventual consistency without trusting vectors.
"""

from __future__ import annotations

import logging
from typing import Any

from src.memory.models import MemoryRecord
from src.memory.vectors import VectorIndex
from src.midplatform.ops_store import OpsStore, new_id, now

log = logging.getLogger(__name__)

COLUMNS = (
    "id, user_id, tenant_id, chat_id, scope, kind, subject, attribute, value_text, confidence, "
    "source, evidence_ref, importance, valid_from, valid_to, created_at, status, supersedes_id, "
    "disputed_with, access_count, last_accessed"
)


def _record(row: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(**{key: row[key] for key in COLUMNS.split(", ")})


def vector_payload(record: MemoryRecord) -> dict[str, Any]:
    """Return filterable vector metadata without memory contents."""
    return {
        "user_id": record.user_id,
        "tenant_id": record.tenant_id or "",
        "chat_id": record.chat_id or "",
        "scope": record.scope,
        "kind": record.kind,
        "status": record.status,
    }


def embedding_text(record_or_candidate: Any) -> str:
    """Build embedding text as subject attribute: value."""
    return (
        f"{record_or_candidate.subject} {record_or_candidate.attribute}: "
        f"{record_or_candidate.value_text}"
    )


class MemoryStore:
    """Access memory records and vectors with explicit ownership on reads and deletes."""

    def __init__(self, ops: OpsStore, index: VectorIndex, collection: str):
        self.ops = ops
        self.index = index
        self.collection = collection

    def get(self, memory_id: str, user_id: str) -> MemoryRecord | None:
        """Return an owned memory, or None."""
        row = self.ops.query_one(
            f"SELECT {COLUMNS} FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)
        )
        return _record(row) if row else None

    def get_many(self, ids: list[str], user_id: str) -> list[MemoryRecord]:
        """Fetch only records belonging to the specified user."""
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.ops.query(
            f"SELECT {COLUMNS} FROM memories WHERE user_id = ? AND id IN ({marks})",
            (user_id, *ids),
        )
        return [_record(row) for row in rows]

    def by_slot(
        self, user_id: str, subject: str, attribute: str, statuses: tuple[str, ...]
    ) -> list[MemoryRecord]:
        """Read newest records from an owned normalized slot with selected states."""
        marks = ",".join("?" for _ in statuses)
        rows = self.ops.query(
            f"SELECT {COLUMNS} FROM memories WHERE user_id = ? AND lower(subject) = ? "
            f"AND lower(attribute) = ? AND status IN ({marks}) ORDER BY valid_from DESC",
            (user_id, subject, attribute, *statuses),
        )
        return [_record(row) for row in rows]

    def list_for_user(
        self, user_id: str, *, include_inactive: bool = False, limit: int = 200
    ) -> list[MemoryRecord]:
        """List newest owned memories with a limit and optional inactive history."""
        clause = "" if include_inactive else "AND status IN ('active','disputed')"
        rows = self.ops.query(
            f"SELECT {COLUMNS} FROM memories WHERE user_id = ? {clause} "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [_record(row) for row in rows]

    def insert(self, record: MemoryRecord, conn) -> None:
        """Insert a record inside the caller's transaction."""
        conn.execute(
            f"INSERT INTO memories ({COLUMNS}) VALUES ({','.join('?' for _ in COLUMNS.split(', '))})",
            tuple(getattr(record, key) for key in COLUMNS.split(", ")),
        )

    def set_status(self, conn, memory_id: str, status: str, **fields: Any) -> None:
        """Update status and trusted column fields inside the caller's transaction."""
        unknown = set(fields) - set(COLUMNS.split(", "))
        if unknown:
            raise ValueError(f"Unknown memory columns: {sorted(unknown)}")
        assignments = ", ".join(["status = ?"] + [f"{key} = ?" for key in fields])
        conn.execute(
            f"UPDATE memories SET {assignments} WHERE id = ?", (status, *fields.values(), memory_id)
        )

    def _enqueue(self, record_id: str, action: str) -> None:
        self.ops.execute(
            "INSERT INTO vector_outbox VALUES (?,?,?,?,?)",
            (new_id(), self.collection, record_id, action, now()),
        )

    def sync_vectors(self, records: list[MemoryRecord], vectors: dict[str, list[float]]) -> None:
        """Mirror state and visibility into vectors, queueing failures."""
        fallback = getattr(self.index, "fallback", False)
        for record in records:
            if fallback:
                self._enqueue(
                    record.id, "upsert" if record.status in ("active", "disputed") else "delete"
                )
            try:
                if record.status in ("active", "disputed") and record.id in vectors:
                    self.index.upsert(record.id, vectors[record.id], vector_payload(record))
                elif record.status not in ("active", "disputed"):
                    self.index.delete([record.id])
            except Exception:  # noqa: BLE001 - The outbox ensures eventual consistency.
                log.exception("Vector index write failed for memory %s; queued", record.id)
                action = "upsert" if record.status in ("active", "disputed") else "delete"
                self.ops.execute(
                    "INSERT INTO vector_outbox VALUES (?,?,?,?,?)",
                    (new_id(), self.collection, record.id, action, now()),
                )

    async def replay_outbox(self, embedder) -> int:
        """Retry queued vector writes and return the success count."""
        if getattr(self.index, "fallback", False):
            # Process-local index success is not durable delivery; retain every queued task
            # until real Qdrant recovers, including maintenance during the same outage.
            return 0
        rows = self.ops.query(
            "SELECT * FROM vector_outbox WHERE collection = ? ORDER BY created_at",
            (self.collection,),
        )
        done = 0
        for row in rows:
            try:
                record_row = self.ops.query_one(
                    f"SELECT {COLUMNS} FROM memories WHERE id = ?", (row["point_id"],)
                )
                if record_row and record_row["status"] in ("active", "disputed"):
                    record = _record(record_row)
                    vector = (await embedder.embed([embedding_text(record)]))[0]
                    self.index.upsert(record.id, vector, vector_payload(record))
                else:
                    self.index.delete([row["point_id"]])
                self.ops.execute("DELETE FROM vector_outbox WHERE id = ?", (row["id"],))
                done += 1
            except Exception:  # noqa: BLE001
                log.warning("Outbox replay still failing for %s", row["point_id"])
        return done

    def touch(self, ids: list[str]) -> None:
        """Increment recall count and refresh last-access time."""
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.ops.execute(
            f"UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({marks})",
            (now(), *ids),
        )

    def forget(self, user_id: str, memory_id: str | None = None) -> int:
        """Delete an owned version chain or all of a user's memories."""
        if memory_id:
            chain = self._version_chain(user_id, memory_id)
        else:
            chain = [
                row["id"]
                for row in self.ops.query("SELECT id FROM memories WHERE user_id = ?", (user_id,))
            ]
        if not chain:
            return 0
        marks = ",".join("?" for _ in chain)
        self.ops.execute(
            f"DELETE FROM memories WHERE user_id = ? AND id IN ({marks})", (user_id, *chain)
        )
        try:
            self.index.delete(chain)
        except Exception:  # noqa: BLE001
            for record_id in chain:
                self.ops.execute(
                    "INSERT INTO vector_outbox VALUES (?,?,?,?,?)",
                    (new_id(), self.collection, record_id, "delete", now()),
                )
        return len(chain)

    def _version_chain(self, user_id: str, memory_id: str) -> list[str]:
        """Follow version and resolved-conflict links within one owner."""
        seen: set[str] = set()
        frontier = [memory_id]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            row = self.ops.query_one(
                "SELECT id, supersedes_id, disputed_with FROM memories WHERE id = ? AND user_id = ?",
                (current, user_id),
            )
            if not row:
                continue
            seen.add(current)
            if row["supersedes_id"]:
                frontier.append(row["supersedes_id"])
            if row["disputed_with"]:
                frontier.append(row["disputed_with"])
            frontier.extend(
                r["id"]
                for r in self.ops.query(
                    "SELECT id FROM memories WHERE (supersedes_id = ? OR disputed_with = ?) AND user_id = ?",
                    (current, current, user_id),
                )
            )
        return list(seen)
