"""Successful tools must not hide missing business deliverables."""

import pytest

from src.agent.grounding import EvidenceLedger


@pytest.mark.parametrize(
    "formatting", ["Round the final percentage to 0 decimal places.", "保留0位小数。"]
)
def test_presentation_precision_cannot_fabricate_a_zero_business_answer(formatting):
    ledger = EvidenceLedger(required={"kb"}, prompt="What percentage is current? " + formatting)
    ledger.add("kb", [{"content": "Next-year maturity 1127; total recourse debt 11572."}])
    assert any(
        "Unsupported numeric claims: 0" in issue
        for issue in ledger.issues("Current debt: 0%. [KB1]")
    )


def evidence():
    """Provide evidence test data or behavior for this scenario."""
    ledger = EvidenceLedger(required={"db", "db_count", "db_total"})
    ledger.add(
        "db",
        [{"order_count": 2, "total_amount": 2000}],
        sql="SELECT COUNT(DISTINCT order_id) AS order_count, SUM(amount) AS total_amount FROM sales",
    )
    return ledger


def test_tool_completion_is_not_answer_completion():
    ledger = evidence()
    assert not ledger.missing()
    assert any(
        "Missing requested answer field: count" in issue
        for issue in ledger.issues("总金额2000元。")
    )
    assert any(
        "Missing requested answer field: amount" in issue for issue in ledger.issues("订单数2。")
    )


@pytest.mark.parametrize(
    "answer",
    [
        "订单数：2。总金额：2000元。",
        "**订单数**：**2**；**总金额**：**2,000**。",
        '{"order_count": 2, "total_amount": 2000}',
        "| 订单数 | 2 |\n| 总金额 | 2000 |",
        "Order count: 2. Total amount: 2000.",
    ],
)
def test_explicit_supported_summary_formats(answer):
    assert not evidence().issues(answer)


def test_same_number_elsewhere_does_not_satisfy_count():
    assert evidence().issues("共返回2行，金额2000。总金额：2000元。")
    assert evidence().issues("订单数：2000。总金额：2000。")


def test_post_discount_total_is_a_separate_measure():
    from decimal import Decimal

    from src.agent.answer_contract import summary_issues

    assert not summary_issues(
        "订单数2。总金额2000元；折后总金额1880元。",
        {"db_count", "db_total"},
        {Decimal(2)},
        {Decimal(2000)},
    )


def test_unavailable_measure_is_not_forced_into_answer():
    ledger = EvidenceLedger(required={"db_count"})
    assert not any("answer field" in issue for issue in ledger.issues("未能获得计数依据。"))


def test_item_rows_and_distinct_orders_are_separate_verified_counts():
    from src.agent.answer_contract import summary_issues

    assert not summary_issues(
        "不同订单数：2。记录数：3。", {"db_count"}, {2}, set(), result_row_counts={3}
    )
    assert summary_issues("订单数：3。记录数：3。", {"db_count"}, {2}, set(), result_row_counts={3})
    assert summary_issues("记录数：3。", {"db_count"}, {2}, set(), result_row_counts={3})
    assert summary_issues("订单数：2。记录数：4。", {"db_count"}, {2}, set(), result_row_counts={3})


def test_document_only_metric_definition_does_not_require_or_expose_database_tools():
    import asyncio
    from types import SimpleNamespace

    from src.agent.grounding import EvidenceLedger, requested_capabilities
    from src.agent.metric_results import prepare_business_tools

    prompt = "仅检索我刚上传的 seller-metrics.md 文档，解释订单数如何计数、是否等于付款总额。不要查询业务数据库，不要导出。"
    required = requested_capabilities(prompt, True)
    assert required == {"kb"}
    ctx = SimpleNamespace(
        deps=SimpleNamespace(ledger=EvidenceLedger(prompt=prompt, required=required))
    )
    tools = [
        SimpleNamespace(name=name)
        for name in [
            "query_database",
            "query_business_metrics",
            "search_knowledge_documents",
            "search_web_engine",
            "inspect_local_file",
            "mcp_0_public_sum",
        ]
    ]
    assert [tool.name for tool in asyncio.run(prepare_business_tools(ctx, tools))] == [
        "search_knowledge_documents"
    ]
    ctx.deps.ledger.prompt = (
        "仅检索当前授权的企业销售制度，回答折扣上限。不查询业务数据库，不联网搜索。"
    )
    assert requested_capabilities(ctx.deps.ledger.prompt, False) == {"kb"}
    assert [tool.name for tool in asyncio.run(prepare_business_tools(ctx, tools))] == [
        "search_knowledge_documents"
    ]
