"""Fixed synthetic dependencies for shareable agent evaluation.

Synthetic provenance never authorizes production access. Use isolated SQLite sources and
fictional documents; reject caller-supplied DSNs, DDL, paths, and real knowledge IDs.
This evaluates orchestration, not real RAG recall.
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
    """Disposable isolated business and document sources for evaluation."""

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
        """Check the source-file hash to confirm evaluation did not mutate synthetic data."""
        return hashlib.sha256(self.source.read_bytes()).hexdigest() == self.original_hash

    def close(self):
        """Remove the temporary directory holding the synthetic source."""
        self._directory.cleanup()
