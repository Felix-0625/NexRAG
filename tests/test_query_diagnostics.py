"""Filter diagnostics must use the query's actual tenant snapshot."""

import asyncio
import sqlite3

import pytest

from src.agent import tools
from src.agent.business_session import business_read_session
from src.midplatform import adapters
from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM sales WHERE status='completed'",
        "SELECT COUNT(*) n, SUM(amount) total FROM sales WHERE order_id='invented'",
    ],
)
def test_empty_filter_diagnoses_only_same_snapshot_and_authorized_scope(
    tmp_path, monkeypatch, query
):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as owner:
        owner.execute("CREATE TABLE sales(tenant TEXT, order_id TEXT, status TEXT, amount REAL)")
        owner.executemany(
            "INSERT INTO sales VALUES(?,?,?,?)",
            [("a", "A-1", "已完成", 1), ("b", "B-1", "已完成", 10)],
        )
    adapter = SQLiteAdapter(
        profile=SQLiteSourceProfile(
            path,
            None,
            {
                "sales": ViewSpec(
                    {"order_id": "TEXT", "status": "TEXT", "amount": "REAL"},
                    "SELECT tenant AS __tenant_id,order_id,status,amount FROM sales",
                )
            },
        )
    )
    monkeypatch.setattr(adapters, "create_business_adapter", lambda _: adapter)

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        async with business_read_session():
            first = await asyncio.to_thread(tools.query_business, query, "b", None)
            with sqlite3.connect(path) as owner:
                owner.execute("UPDATE sales SET amount=999 WHERE tenant='b'")
            second = await asyncio.to_thread(tools.query_business, "SELECT * FROM sales", "b", None)
            return first, second

    first, second = asyncio.run(run())
    assert first["authorized_view_samples"]["sales"] == [
        {"order_id": "B-1", "status": "已完成", "amount": 10}
    ]
    assert first["snapshot_id"] == second["snapshot_id"]
    assert second["rows"][0]["amount"] == 10
    assert "NOT empty" in first["reflection_hint"]
