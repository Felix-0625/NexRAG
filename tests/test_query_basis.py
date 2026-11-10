"""Request-grounded predicates supplement SQL authorization and grain validation."""

import asyncio
from types import SimpleNamespace

import pytest

from src.agent.grounding import EvidenceLedger
from src.agent.metric_results import prepare_business_tools
from src.agent.query_basis import require_query_filter_basis


@pytest.mark.parametrize(
    "sql,prompt",
    [
        ("SELECT SUM(amount) FROM sales WHERE status='completed'", "查询订单总金额"),
        ("SELECT * FROM sales WHERE 0 < amount", "查询订单明细"),
        ("SELECT * FROM sales WHERE amount BETWEEN 10 AND 20", "查询金额大于10的订单"),
        ("SELECT * FROM sales WHERE status IN ('已完成','已取消')", "仅查询已完成订单"),
    ],
)
def test_unrequested_direct_filter_values_are_rejected(sql, prompt):
    with pytest.raises(ValueError, match="未执行查询"):
        require_query_filter_basis(sql, prompt)


@pytest.mark.parametrize(
    "sql,prompt",
    [
        ("SELECT SUM(amount) FROM sales", "查询订单总金额"),
        ("SELECT * FROM sales WHERE amount > 100 LIMIT 50", "查询金额大于100元的订单"),
        ("SELECT * FROM sales WHERE status='已完成'", "查询已完成订单"),
        ("SELECT * FROM sales WHERE status IN ('已完成','已取消')", "查询已完成和已取消订单"),
        ("SELECT * FROM sales WHERE amount >= -20", "查询金额不低于-20的订单"),
        ("SELECT * FROM sales WHERE product LIKE '%软件%'", "查询包含软件的商品"),
    ],
)
def test_explicit_filter_values_and_row_limits_are_preserved(sql, prompt):
    require_query_filter_basis(sql, prompt)


def test_empty_catalog_hides_metric_tool_without_disabling_raw_reads():
    ledger = EvidenceLedger(
        required={"db"}, prompt="查询业务总额", registered_metric_ids=frozenset()
    )
    context = SimpleNamespace(deps=SimpleNamespace(ledger=ledger))
    tools = [SimpleNamespace(name=name) for name in ["query_database", "query_business_metrics"]]
    assert [tool.name for tool in asyncio.run(prepare_business_tools(context, tools))] == [
        "query_database"
    ]
    ledger.registered_metric_ids = frozenset({"order_total"})
    assert asyncio.run(prepare_business_tools(context, tools)) == tools


def test_total_of_all_orders_does_not_invent_a_detail_deliverable():
    from src.agent.grounding import requested_capabilities

    for prompt in ["查询全部订单总金额", "查询全部订单的总金额"]:
        assert requested_capabilities(prompt, True) == {"db", "db_total"}
    assert "db_details" in requested_capabilities("列出全部订单以及总金额", True)
