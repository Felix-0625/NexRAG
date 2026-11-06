"""ops store 中的权威记忆记录及其向量索引。

先在事务内写 SQLite；向量索引写入失败时排入 ``vector_outbox``，由
``replay_outbox`` 重放，使两个存储最终一致；召回时始终回 SQLite 复核状态/可见性。
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
    """镜像到向量索引中的可过滤字段（不含记忆正文）。"""
    return {
        "user_id": record.user_id,
        "tenant_id": record.tenant_id or "",
        "chat_id": record.chat_id or "",
        "scope": record.scope,
        "kind": record.kind,
        "status": record.status,
    }


def embedding_text(record_or_candidate: Any) -> str:
    """生成用于向量化的文本，格式为“主体 属性: 值”。"""
    return (
        f"{record_or_candidate.subject} {record_or_candidate.attribute}: "
        f"{record_or_candidate.value_text}"
    )


class MemoryStore:
    def __init__(self, ops: OpsStore, index: VectorIndex, collection: str):
        self.ops = ops
        self.index = index
        self.collection = collection

    def get(self, memory_id: str, user_id: str) -> MemoryRecord | None:
        """按 ID 取记忆；记录必须属于该用户，否则视同不存在并返回 None。"""
        row = self.ops.query_one(
            f"SELECT {COLUMNS} FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)
        )
        return _record(row) if row else None

    def get_many(self, ids: list[str], user_id: str) -> list[MemoryRecord]:
        """批量取记忆，只返回属于该用户的记录。"""
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
        """取该用户同一槽位（主体、属性不区分大小写，传入值需已规范化为小写）且处于指定状态的记忆，新的在前。"""
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
        """列出该用户的记忆，按创建时间倒序，最多 limit 条；默认只含 active 和 disputed，include_inactive 时含历史记录。"""
        clause = "" if include_inactive else "AND status IN ('active','disputed')"
        rows = self.ops.query(
            f"SELECT {COLUMNS} FROM memories WHERE user_id = ? {clause} "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [_record(row) for row in rows]

    def insert(self, record: MemoryRecord, conn) -> None:
        """在调用方的事务内插入记录。"""
        conn.execute(
            f"INSERT INTO memories ({COLUMNS}) VALUES ({','.join('?' for _ in COLUMNS.split(', '))})",
            tuple(getattr(record, key) for key in COLUMNS.split(", ")),
        )

    def set_status(self, conn, memory_id: str, status: str, **fields: Any) -> None:
        """在调用方的事务内更新记忆状态，并可同时更新其他列（fields 的键为列名，须由调用方保证可信）。"""
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
        """将状态/可见性镜像到向量索引；失败的写入进入队列。"""
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
            except Exception:  # noqa: BLE001 - outbox 保证最终一致
                log.exception("Vector index write failed for memory %s; queued", record.id)
                action = "upsert" if record.status in ("active", "disputed") else "delete"
                self.ops.execute(
                    "INSERT INTO vector_outbox VALUES (?,?,?,?,?)",
                    (new_id(), self.collection, record.id, action, now()),
                )

    async def replay_outbox(self, embedder) -> int:
        """重试排队中的向量写入，返回成功条数。"""
        if getattr(self.index, "fallback", False):
            # 写入进程内索引成功并不等于持久送达。真实 Qdrant 可达之前，
            # 所有任务都保持排队，包括与写入同处一次故障期间的维护运行。
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
        """记录记忆被召回：访问次数加一并刷新最近访问时间。"""
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.ops.execute(
            f"UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({marks})",
            (now(), *ids),
        )

    def forget(self, user_id: str, memory_id: str | None = None) -> int:
        """物理删除一条记忆（连同其版本链），或删除某用户的全部记忆。"""
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
        """沿版本与已解决冲突的链接遍历，范围限于同一所有者。"""
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
