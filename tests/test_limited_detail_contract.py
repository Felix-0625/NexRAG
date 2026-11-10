"""Explicitly bounded details satisfy samples, never whole-source aggregates."""

import asyncio
import sqlite3

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.agent.agent import AgenticRunner, UserContext
from src.agent.detail_contract import completes_detail_request, requested_detail_limit
from src.agent.grounding import EvidenceLedger, requested_capabilities
from src.agent.metric_results import (
    business_report_ready,
    render_business_body,
    report_from_evidence,
)
from src.agent.run_options import EvaluationRun
from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec

PROMPT = (
    "只查询当前获授权业务数据库的 orders 表，列出1条订单的 order_id。"
    "不要统计、不查知识库、不联网，不调用外部工具。请使用实际SQL查询结果回答。"
)
IDENTIFIER = "002f98c0f7efd42638ed6100ca699b42"


def evidence(prompt=PROMPT, sql="SELECT order_id FROM seller_orders LIMIT 1", rows=None):
    """Provide evidence test data or behavior for this scenario."""
    ledger = EvidenceLedger(prompt=prompt, required=requested_capabilities(prompt, True))
    ledger.add("db", [{"order_id": IDENTIFIER}] if rows is None else rows, sql=sql)
    return ledger


def test_original_request_succeeds_through_actual_agent_snapshot_and_report(tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(tenant_id TEXT, order_id TEXT, amount INTEGER)")
        connection.executemany(
            "INSERT INTO orders VALUES(?,?,?)",
            [("a", IDENTIFIER, 280), ("a", "other", 120), ("b", "private-b", 900)],
        )
    view = ViewSpec(
        {"order_id": "TEXT", "amount": "INTEGER"},
        "SELECT tenant_id AS __tenant_id, order_id, amount FROM orders",
        ("order_id",),
        "order",
    )
    adapter = SQLiteAdapter(profile=SQLiteSourceProfile(path, None, {"seller_orders": view}))
    calls = 0

    def model(messages, info):
        """Provide deterministic model responses for this scenario."""
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                [
                    ToolCallPart(
                        "query_database", {"sql": "SELECT order_id FROM seller_orders LIMIT 1"}
                    )
                ]
            )
        assert calls == 2 and not info.function_tools and info.output_tools
        # Render actual bounded evidence even when the model transcribes it incorrectly.
        return ModelResponse([TextPart("订单号：invented-other-account。")])

    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))
    evaluation = EvaluationRun(
        "",
        {},
        authorized_tenants=("a",),
        business_adapter=adapter,
        business_schema="Authorized view: seller_orders(order_id, amount).",
    )

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                PROMPT,
                UserContext(user_id="bounded-test", email="bounded@local.test", tenant_id="a"),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert calls == 2 and evaluation.status == "ok"
    assert IDENTIFIER in answer and "private-b" not in answer and "invented" not in answer
    assert "限制为1条明细" in answer and "未完成" not in answer


@pytest.mark.parametrize(
    "sql,rows",
    [
        ("SELECT order_id FROM seller_orders LIMIT 0", []),
        ("SELECT order_id FROM seller_orders LIMIT 2", [{"order_id": IDENTIFIER}]),
        ("SELECT order_id FROM seller_orders LIMIT 1 OFFSET 0", [{"order_id": IDENTIFIER}]),
        ("SELECT order_id FROM seller_orders", [{"order_id": IDENTIFIER}]),
        ("SELECT COUNT(order_id) AS order_id FROM seller_orders LIMIT 1", [{"order_id": 1}]),
        (
            "SELECT order_id, COUNT(*) OVER () AS count FROM seller_orders LIMIT 1",
            [{"order_id": IDENTIFIER, "count": 2}],
        ),
        ("SELECT 'fake' AS order_id FROM seller_orders LIMIT 1", [{"order_id": "fake"}]),
        ("SELECT amount AS order_id FROM seller_orders LIMIT 1", [{"order_id": 280}]),
        (
            "SELECT order_id FROM seller_orders LIMIT 1",
            [{"order_id": IDENTIFIER}, {"order_id": "extra"}],
        ),
    ],
)
def test_invalid_sample_does_not_complete_or_enter_report_phase(sql, rows):
    ledger = evidence(sql=sql, rows=rows)
    assert "db_details" in ledger.missing()
    assert not business_report_ready(ledger)
    assert not report_from_evidence(ledger).detail_evidence_ids


def test_three_requested_rows_cannot_be_replaced_by_limit_one_but_can_exhaust_at_two():
    prompt = PROMPT.replace("列出1条", "列出3条")
    assert "db_details" in evidence(prompt=prompt).missing()
    ledger = evidence(
        prompt=prompt,
        sql="SELECT order_id FROM seller_orders LIMIT 3",
        rows=[{"order_id": IDENTIFIER}, {"order_id": "second"}],
    )
    assert not ledger.missing()
    assert not ledger.issues(render_business_body(report_from_evidence(ledger), ledger))


def test_alias_and_empty_results_keep_actual_projection_provenance():
    ledger = evidence(
        sql="SELECT order_id AS identifier FROM seller_orders LIMIT 1",
        rows=[{"identifier": IDENTIFIER}],
    )
    assert not ledger.missing()
    assert IDENTIFIER in render_business_body(report_from_evidence(ledger), ledger)
    empty = evidence(rows=[])
    assert not empty.missing()
    assert "返回0行" in render_business_body(report_from_evidence(empty), empty)


@pytest.mark.parametrize(
    "prompt",
    [
        "查询订单明细order_id",
        "列出全部订单的order_id",
        "列出最多3条订单的order_id",
        "不要列出1条订单，查询全部订单的order_id",
        "列出0条订单的order_id",
        "列出51条订单的order_id",
        "列出金额最高的前1条订单的order_id",
        "按金额降序列出前1条订单的order_id",
    ],
)
def test_no_supported_explicit_sample_does_not_relax_full_detail_contract(prompt):
    assert requested_detail_limit(prompt) is None
    ledger = evidence(prompt=prompt)
    assert not completes_detail_request(ledger.entries[0], prompt)


def test_sample_never_completes_count_total_or_mixed_grain_obligations():
    ledger = evidence()
    ledger.required |= {"db_count", "db_total", "db_join:items,orders", "db_field:orders.amount"}
    assert ledger.missing() == {
        "db_count",
        "db_total",
        "db_join:items,orders",
        "db_field:orders.amount",
    }
    assert not business_report_ready(ledger)
    assert ledger.export_issues(None, "订单总数：1。总金额：280。")
