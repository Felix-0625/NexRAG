"""真实的数据源更新、并发读取、清理与租户隔离。"""

import asyncio
import sqlite3

import pytest

from src.agent.business_session import business_read_session
from src.midplatform import adapters
from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as owner:
        owner.execute("CREATE TABLE sales(tenant TEXT, id TEXT, amount REAL)")
        owner.executemany(
            "INSERT INTO sales VALUES(?,?,?)",
            [("a", "A1", 1250), ("a", "A2", 750), ("b", "B1", 9900)],
        )
    adapter = SQLiteAdapter(
        profile=SQLiteSourceProfile(
            path,
            None,
            {
                "sales": ViewSpec(
                    {"id": "TEXT", "amount": "REAL"},
                    "SELECT tenant AS __tenant_id,id,amount FROM sales",
                    ("id",),
                    "order",
                )
            },
        )
    )
    monkeypatch.setattr(adapters, "create_business_adapter", lambda _: adapter)
    return path, adapter


def test_one_turn_is_consistent_next_turn_is_fresh_and_snapshot_closes(source):
    path, adapter = source

    async def run():
        async with business_read_session() as session:
            first = await asyncio.to_thread(
                session.read, "SELECT id,amount FROM sales ORDER BY id", "a"
            )
            with sqlite3.connect(path) as owner:
                owner.execute("UPDATE sales SET amount=1500 WHERE id='A1'")
            second = await asyncio.to_thread(
                session.read, "SELECT SUM(amount) total FROM sales", "a"
            )
            assert second["rows"] == [{"total": 2000}]
            assert first["snapshot_id"] == second["snapshot_id"]
            with pytest.raises(ValueError, match="another tenant"):
                session.read("SELECT * FROM sales", "b")
            for sql in ["UPDATE sales SET amount=0", "DELETE FROM sales", "ATTACH 'x.db' AS x"]:
                with pytest.raises(ValueError):
                    session.read(sql, "a")
            snapshot = session.snapshot
        with pytest.raises(ValueError, match="closed"):
            snapshot.execute_query("SELECT * FROM sales", "a")
        async with business_read_session() as new_session:
            fresh = await asyncio.to_thread(
                new_session.read, "SELECT SUM(amount) total FROM sales", "a"
            )
            assert fresh["rows"] == [{"total": 2250}]
            assert fresh["snapshot_id"] != first["snapshot_id"]

    asyncio.run(run())
    assert adapter.execute_query("SELECT * FROM sales", "b") == [{"id": "B1", "amount": 9900}]


def test_parallel_tools_create_one_snapshot_and_failure_closes_it(source):
    async def run():
        session = None
        with pytest.raises(RuntimeError, match="synthetic run failure"):
            async with business_read_session() as session:
                rows = await asyncio.gather(
                    *[
                        asyncio.to_thread(session.read, "SELECT SUM(amount) total FROM sales", "a")
                        for _ in range(4)
                    ]
                )
                assert len({item["snapshot_id"] for item in rows}) == 1
                raise RuntimeError("synthetic run failure")
        with pytest.raises(ValueError, match="ended"):
            session.read("SELECT * FROM sales", "a")

    asyncio.run(run())


def test_production_agent_tools_reuse_snapshot_and_close_after_model_run(source, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from src.agent.agent import AgenticRunner, UserContext
    from src.agent.business_session import current_business_session
    from src.midplatform.tenant_access import create_tenant, set_grant

    path, _ = source
    create_tenant("a", "Synthetic A")
    set_grant("snapshot@local", "a", True, "test")
    runner = AgenticRunner()
    runner._agent = runner._build_agent()
    recorded = []
    monkeypatch.setattr(runner, "_finish", lambda trace, *a, **kw: recorded.append(trace))
    observed, sessions = [], []
    step = 0

    def model(messages, info):
        nonlocal step
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == "query_database":
                    if part.content not in observed:
                        observed.append(part.content)
        sessions.append(current_business_session())
        step += 1
        if step == 1:
            return ModelResponse(
                [ToolCallPart("query_database", {"sql": "SELECT id,amount FROM sales ORDER BY id"})]
            )
        if step == 2:
            with sqlite3.connect(path) as owner:
                owner.execute("UPDATE sales SET amount=1500 WHERE id='A1'")
            return ModelResponse(
                [
                    ToolCallPart(
                        "query_database", {"sql": "SELECT SUM(amount) total_amount FROM sales"}
                    )
                ]
            )
        return ModelResponse([TextPart("A1：1250，A2：750。总金额：2000。")])

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "查询数据库明细和总金额",
                UserContext(user_id="snapshot", email="snapshot@local", tenant_id="a"),
            )

    assert "2000" in asyncio.run(run())
    assert len(observed) == 2
    assert observed[0]["snapshot_id"] == observed[1]["snapshot_id"]
    assert recorded[0].artifact_versions["business_snapshot"] == observed[0]["snapshot_id"]
    assert observed[1]["rows"] == [{"total_amount": 2000}]
    assert all(session is sessions[0] for session in sessions)
    with pytest.raises(ValueError, match="closed"):
        sessions[0].snapshot.execute_query("SELECT * FROM sales", "a")
    # 生产轮次已结束；独立发起的新读取能看到所有者的更新。
    assert source[1].execute_query("SELECT SUM(amount) total FROM sales", "a") == [{"total": 2250}]
