"""可写的运维数据域：trace、分数、数据集、记忆与产物。

业务数据留在只读的租户快照适配器中。运维记录存放在单独的 SQLite 文件
（``settings.OPS_DB_PATH``）里，Agent 的 SQL 工具永远访问不到，只有服务端代码读写。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY, user_hash TEXT NOT NULL, tenant_id TEXT, chat_id TEXT,
    artifact_versions TEXT NOT NULL DEFAULT '{}', canary INTEGER NOT NULL DEFAULT 0,
    input_text TEXT, output_text TEXT, latency_ms INTEGER, status TEXT NOT NULL,
    error TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_traces_created ON traces(created_at);
CREATE INDEX IF NOT EXISTS ix_traces_chat ON traces(chat_id);
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, type TEXT NOT NULL, name TEXT NOT NULL,
    input_summary TEXT, output_summary TEXT, error TEXT, position INTEGER NOT NULL,
    FOREIGN KEY(trace_id) REFERENCES traces(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_obs_trace ON observations(trace_id);
CREATE TABLE IF NOT EXISTS scores (
    id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL,
    data_type TEXT NOT NULL, source TEXT NOT NULL, comment TEXT, created_at REAL NOT NULL,
    FOREIGN KEY(trace_id) REFERENCES traces(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_scores_trace ON scores(trace_id);
CREATE INDEX IF NOT EXISTS ix_scores_name ON scores(name, source);
"""
SCHEMA += """
CREATE TABLE IF NOT EXISTS dataset_items (
    id TEXT PRIMARY KEY, dataset TEXT NOT NULL, split TEXT NOT NULL, category TEXT NOT NULL,
    input TEXT NOT NULL, expected TEXT NOT NULL, origin_trace_id TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_split ON dataset_items(dataset, split);
CREATE TABLE IF NOT EXISTS experiment_runs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, artifact_versions TEXT NOT NULL,
    metrics TEXT NOT NULL, item_scores TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_versions (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, parent_id TEXT,
    label TEXT NOT NULL, note TEXT, created_by TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifacts_kind ON artifact_versions(kind, label);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, cluster_key TEXT NOT NULL,
    candidate_id TEXT NOT NULL, baseline_id TEXT, status TEXT NOT NULL,
    evidence_count INTEGER NOT NULL, report TEXT NOT NULL, priority REAL NOT NULL,
    reviewer TEXT, review_note TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    canary_started_at REAL
);
CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(kind, status);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT, chat_id TEXT,
    scope TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, attribute TEXT NOT NULL,
    value_text TEXT NOT NULL, confidence REAL NOT NULL, source TEXT NOT NULL,
    evidence_ref TEXT, importance REAL NOT NULL, valid_from REAL NOT NULL, valid_to REAL,
    created_at REAL NOT NULL, status TEXT NOT NULL, supersedes_id TEXT,
    disputed_with TEXT, access_count INTEGER NOT NULL DEFAULT 0, last_accessed REAL
);
CREATE INDEX IF NOT EXISTS ix_mem_slot ON memories(user_id, subject, attribute, status);
CREATE INDEX IF NOT EXISTS ix_mem_scope ON memories(user_id, scope, tenant_id, chat_id);
CREATE TABLE IF NOT EXISTS vector_outbox (
    id TEXT PRIMARY KEY, collection TEXT NOT NULL, point_id TEXT NOT NULL,
    action TEXT NOT NULL, created_at REAL NOT NULL
);
-- Admin-maintained shared knowledge base. NexRAG has no direct Qdrant read
-- path for this collection, so this table is the only inventory of what is
-- in it; file_id must match what the ingestion service indexed chunks under.
CREATE TABLE IF NOT EXISTS knowledge_documents (
    file_id TEXT PRIMARY KEY, filename TEXT NOT NULL, collection_name TEXT NOT NULL,
    chunks_count INTEGER NOT NULL, uploaded_by TEXT NOT NULL, created_at REAL NOT NULL
);
-- Admin-managed business tenant catalogue and per-user grants, replacing
-- the deployer-maintained tenant-access.yaml as the live source of truth.
-- The YAML file is only used once, to seed this table on first startup
-- (see src.midplatform.tenant_access.seed_from_yaml), so existing
-- deployments and tests that only ship the YAML keep working.
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_grants (
    email TEXT NOT NULL, tenant_id TEXT NOT NULL, granted_by TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY (email, tenant_id)
);
CREATE TABLE IF NOT EXISTS workspace_settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_connections (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
    config TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def new_id() -> str:
    """为运维记录返回一个随机标识符。"""
    return uuid.uuid4().hex


def now() -> float:
    """返回当前挂钟时间（epoch 秒）。"""
    return time.time()


def dumps(value: Any) -> str:
    """序列化 JSON 列的值。"""
    return json.dumps(value, ensure_ascii=False, default=str)


class OpsStore:
    """运维数据域的线程安全 SQLite 仓库。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or settings.OPS_DB_PATH)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """原子地执行多条语句；出现任何异常即回滚。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        """在独立事务中执行一条写语句。"""
        with self.transaction() as conn:
            conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        """以字典列表形式返回读查询的所有行。"""
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        """返回读查询的第一行；没有则返回 ``None``。"""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        """关闭底层连接。"""
        with self._lock:
            self._conn.close()


_store: OpsStore | None = None
_store_lock = threading.Lock()


def get_ops_store() -> OpsStore:
    """返回进程级的运维 store，惰性创建。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = OpsStore()
        return _store


def set_ops_store(store: OpsStore | None) -> None:
    """替换进程级 store（供测试和脚本使用）。"""
    global _store
    with _store_lock:
        _store = store
