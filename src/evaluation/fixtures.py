"""可共享的 Agent 评估所用的固定合成依赖。

`provenance=synthetic` 只是数据声明，并不授权查询生产数据。
因此公开的评估流程改用隔离的 SQLite 数据源和固定的虚构文档，
不接受调用方提供的 DSN、建表 SQL、文件路径或真实知识库 ID。
这里测试的是 Agent 编排，而不是真实 RAG 的召回。
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec

TENANT_IDS = ("synthetic-a", "synthetic-b")
SCHEMA_TEXT = """Synthetic sales view (already scoped by the server):
sales(order_id TEXT, amount REAL). amount is in currency units.
Query the view directly. It has no tenant_id column. Compute sums in SQL.
"""
DOCUMENTS = [
    {
        "source": "synthetic-sales-policy.md",
        "content": "Synthetic company policy: discount limit 6%, approver Demo Chen. POLICY_SYNTHETIC_937.",
        "score": 1.0,
    },
]


class SyntheticFixture:
    def __init__(self):
        self._directory = TemporaryDirectory(prefix="nexrag-synthetic-eval-")
        source = Path(self._directory.name) / "business.db"
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute("CREATE TABLE sales (tenant_id TEXT,order_id TEXT,amount REAL)")
            connection.executemany(
                "INSERT INTO sales VALUES (?,?,?)",
                [
                    ("synthetic-a", "SYN-A-001", 100),
                    ("synthetic-a", "SYN-A-002", 250),
                    ("synthetic-b", "SYN-B-001", 9900),
                ],
            )
        profile = SQLiteSourceProfile(
            source,
            None,
            {
                "sales": ViewSpec(
                    {"order_id": "TEXT", "amount": "REAL"},
                    "SELECT tenant_id AS __tenant_id,order_id,amount FROM sales",
                ),
            },
        )
        self.adapter = SQLiteAdapter(profile=profile)
        self.source = source
        self.original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    def unchanged(self):
        """合成源库文件的哈希是否与创建时一致，用于确认评估运行没有改动数据。"""
        return hashlib.sha256(self.source.read_bytes()).hexdigest() == self.original_hash

    def close(self):
        """清理承载合成源库的临时目录。"""
        self._directory.cleanup()
