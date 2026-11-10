"""Regressions for skipped SQL and fabricated numeric answers."""

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from src.agent import core_tools
from src.agent.agent import AgenticRunner, UserContext
from src.agent.grounding import (
    EvidenceLedger,
    GroundingFailure,
    database_write_request,
    requested_capabilities,
)

ROWS = [{"order_id": "B-001", "amount": 9000}, {"order_id": "B-002", "amount": 1000}]


def ledger():
    """Build an evidence ledger for the scenario."""
    evidence = EvidenceLedger(required={"db", "kb"})
    evidence.add("kb", [{"content": "折扣上限6%，审批负责人陈经理。"}])
    return evidence


def test_complex_contract_and_schema_or_reflection_cannot_replace_sql():
    assert requested_capabilities("查询我的订单明细和总金额，对照企业销售制度并导出报告", True) == {
        "db",
        "db_details",
        "db_total",
        "kb",
        "export",
    }
    assert requested_capabilities("收入口径是什么？", True) == set()
    assert requested_capabilities("销售制度的折扣上限是多少？", True) == {"kb"}
    evidence = ledger()
    assert any("db" in issue for issue in evidence.issues("折扣上限6%。"))


@pytest.mark.parametrize(
    "answer",
    [
        "订单ORD-2023-001，金额139200元。",
        "总金额：18000元。",
        "订单数：3。总金额：10000元。",
        "B-003金额9000。",
        "总金额：`18000`元。",
        "```csv\norder_id,amount\nB-999,18000\n```",
    ],
)
def test_fabricated_facts_rejected(answer):
    evidence = ledger()
    evidence.add("db", ROWS, sql="SELECT order_id, amount FROM sales")
    assert evidence.issues(answer)


def test_correct_evidence_and_export_projection():
    evidence = ledger()
    evidence.add("db", ROWS, sql="SELECT order_id, amount FROM sales")
    assert (
        evidence.issues(
            "B-001：9000；B-002：1000。订单数：2。总金额：10000。折扣上限6%，陈经理审批。"
        )
        == []
    )
    assert evidence.export_issues(ROWS, "总金额10000。") == []
    assert evidence.export_issues([{"order_id": "B-003", "amount": 9000}], "")
    assert evidence.export_issues([{"order_id": "B-001", "amount": 1000}], "")


def test_aggregate_does_not_satisfy_requested_details_or_fake_order_count():
    evidence = EvidenceLedger(required={"db", "db_details", "db_count", "db_total"})
    evidence.add(
        "db",
        [{"order_count": 2, "total_amount": 10000}],
        sql="SELECT COUNT(*) AS order_count, SUM(amount) AS total_amount FROM sales",
    )
    assert evidence.missing() == {"db_details"}
    assert "separate plain SELECT" in evidence.retry_hint()
    assert "without COUNT/SUM" in evidence.retry_hint()
    assert evidence.issues("订单数：1。总金额：10000。")
    evidence.add("db", ROWS, sql="SELECT order_id, amount FROM sales")
    assert evidence.missing() == set()


def test_real_agent_retry_falls_back_to_actual_rows(monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from src.midplatform.tenant_access import create_tenant, set_grant

    create_tenant("ui-b", "Synthetic B")
    set_grant("b@local", "ui-b", True, "test")
    monkeypatch.setattr(core_tools, "query_business", lambda *a: {"rows": ROWS, "row_count": 2})
    runner = AgenticRunner()
    runner._agent = runner._build_agent()
    called = False

    def model(messages, info):
        """Provide deterministic model responses for this scenario."""
        nonlocal called
        if not called:
            called = True
            return ModelResponse(
                [ToolCallPart("query_database", {"sql": "SELECT order_id, amount FROM sales"})]
            )
        return ModelResponse([TextPart("总金额18000，订单B-999。")])

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "查询订单的总金额", UserContext(user_id="b", email="b@local", tenant_id="ui-b")
            )

    answer = asyncio.run(run())
    assert "B-001" in answer and "10000" in answer
    assert "18000" not in answer and "B-999" not in answer


def test_missing_sql_remains_failure_after_retries(monkeypatch):
    async def fake_search(*a, **kw):
        """Provide fake search test data or behavior for this scenario."""
        return [{"content": "折扣6%", "source": "制度"}]

    monkeypatch.setattr(core_tools, "search_knowledge", fake_search)
    runner = AgenticRunner()
    runner._agent = runner._build_agent()

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with runner._agent.override(
            model=TestModel(
                call_tools=["search_knowledge_documents"],
                custom_output_text="订单总金额18000。折扣6%。",
            )
        ):
            return await runner.run(
                "查询订单总金额并对照制度",
                UserContext(user_id="b", email="b@local", tenant_id="ui-b"),
            )

    with pytest.raises(GroundingFailure, match="未完成：业务数据库查询"):
        asyncio.run(run())


def test_explicit_database_mutation_refused_before_model():
    assert database_write_request("UPDATE sales SET amount=1")
    assert database_write_request("请把订单B-001金额改为1，删除B-002")
    assert not database_write_request("更新我自己的知识库文档版本")
    answer = asyncio.run(
        AgenticRunner().run(
            "删除业务数据库的订单数据", UserContext(user_id="b", email="b@local", tenant_id="ui-b")
        )
    )
    assert "已拒绝" in answer


@pytest.mark.parametrize(
    "prompt",
    [
        "查询订单明细，再检索企业制度的最新演示更新标识。按制度上限计算订单合计优惠金额。",
        "查看订单更新时间、更新历史和删除标识，查询金额合计。",
        "不要修改业务数据库，只查询订单总金额。",
        "查询订单并更新我自己的知识库文档版本。",
    ],
)
def test_readonly_analysis_is_not_a_database_mutation(prompt):
    assert not database_write_request(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "请更新全部订单金额为0。",
        "把我当前授权部门的全部订单金额都改成0，再删除这些订单。",
        "请在业务数据库中删除订单。",
        "查询订单，然后清空业务数据。",
    ],
)
def test_explicit_imperative_database_mutation_is_detected(prompt):
    assert database_write_request(prompt)


def test_timeout_with_complete_evidence_uses_actual_results(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    runner = AgenticRunner()
    runner._agent = Mock()

    async def prepare(prompt, context):
        """Provide prepare test data or behavior for this scenario."""
        from src.agent.agent import AgentDeps

        return AgentDeps(context=context), SimpleNamespace(versions={}, canary=False)

    async def slow_model(*a, **kw):
        """Provide slow model test data or behavior for this scenario."""
        kw["deps"].ledger.add("db", ROWS, sql="SELECT order_id, amount FROM sales")
        raise TimeoutError()

    runner._prepare = prepare
    runner._agent.run = slow_model
    answer = asyncio.run(
        runner.run("查询订单总金额", UserContext(user_id="b", email="b@local", tenant_id="ui-b"))
    )
    assert "10000" in answer and "B-001" in answer
