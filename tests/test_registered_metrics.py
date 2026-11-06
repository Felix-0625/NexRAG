"""典型业务指标：去重实体、相等金额与租户范围。"""

import asyncio
import hashlib
import sqlite3

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from src.agent.agent import AgenticRunner, UserContext
from src.agent.business_session import BusinessReadSession
from src.agent.metric_results import BusinessReport
from src.agent.prompts import DEFAULT_GUIDANCE
from src.agent.run_options import EvaluationRun
from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec
from src.midplatform.metric_query import MetricFilter, execute_metric_group, metric_queries

DEFINITIONS = {
    "order_count": {
        "table": "orders",
        "calculation": "COUNT(DISTINCT order_id)",
        "description": "订单数",
        "kind": "count",
        "unit": "orders",
    },
    "order_total": {
        "table": "orders",
        "calculation": "SUM(amount)",
        "description": "订单总金额",
        "kind": "amount",
    },
    "item_count": {
        "table": "items",
        "calculation": "COUNT(*)",
        "description": "商品行数",
        "kind": "count",
        "unit": "items",
    },
}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as owner:
        owner.executescript(
            "CREATE TABLE orders(tenant TEXT,id TEXT,amount REAL);"
            "CREATE TABLE items(tenant TEXT,id TEXT,order_id TEXT,price REAL);"
            "INSERT INTO orders VALUES('a','O1',100),('a','O2',100),('b','X1',9900);"
            "INSERT INTO items VALUES('a','I1','O1',40),('a','I2','O1',60),('a','I3','O2',100);"
        )
    adapter = SQLiteAdapter(
        profile=SQLiteSourceProfile(
            path,
            None,
            {
                "orders": ViewSpec(
                    {"order_id": "TEXT", "amount": "REAL"},
                    "SELECT tenant AS __tenant_id,id AS order_id,amount FROM orders",
                    ("order_id",),
                    "orders",
                ),
                "items": ViewSpec(
                    {"item_id": "TEXT", "order_id": "TEXT", "price": "REAL"},
                    "SELECT tenant AS __tenant_id,id AS item_id,order_id,price FROM items",
                    ("item_id",),
                    "items",
                ),
            },
        )
    )
    return path, adapter


def read_metrics(adapter, tenant, names, filters=()):
    session = BusinessReadSession()
    try:
        values = []
        for measures, sql in metric_queries(DEFINITIONS, names, adapter.profile.views, filters):
            _, metrics = execute_metric_group(
                session, tenant, measures, sql, adapter=adapter, filters=filters
            )
            values.extend(metrics)
        return values
    finally:
        session.close()


def test_registered_metrics_keep_entity_counts_and_identical_order_amounts(source):
    path, adapter = source
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    metrics = read_metrics(adapter, "a", list(DEFINITIONS))
    assert {m["metric_id"]: m["value"] for m in metrics} == {
        "order_count": "2",
        "order_total": "200.0",
        "item_count": "3",
    }
    assert len({m["snapshot_id"] for m in metrics}) == 1
    assert {m["scope_id"] for m in metrics} == {"a"}
    assert metrics[1]["unit"] is None  # 不要编造币种。
    assert read_metrics(adapter, "b", ["order_count", "order_total"])[1]["value"] == "9900.0"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_metric_filters_apply_without_silent_omission_or_sql_injection(source):
    _, adapter = source
    metrics = read_metrics(
        adapter,
        "a",
        ["order_count", "order_total"],
        [MetricFilter(field="amount", op="gt", value=100)],
    )
    assert metrics[0]["value"] == "0" and metrics[1]["value"] is None
    with pytest.raises(ValueError, match="过滤字段"):
        metric_queries(
            DEFINITIONS,
            ["order_count", "item_count"],
            adapter.profile.views,
            [MetricFilter(field="amount", value=100)],
        )
    result = read_metrics(
        adapter, "a", ["order_count"], [MetricFilter(field="order_id", value="O1' OR 1=1 --")]
    )
    assert result[0]["value"] == "0"


@pytest.mark.parametrize(
    "calculation",
    [
        "amount + SUM(amount)",
        "(SELECT SUM(amount) FROM orders)",
        "SUM(amount) OVER ()",
        "SUM(items.price)",
        "SUM(amount); DELETE FROM orders",
    ],
)
def test_invalid_metric_definitions_do_not_become_approved_queries(source, calculation):
    _, adapter = source
    definitions = {
        "invalid": {"table": "orders", "calculation": calculation, "description": "Invalid"}
    }
    with pytest.raises(ValueError):
        metric_queries(definitions, ["invalid"], adapter.profile.views)


def test_structured_report_accepts_no_model_supplied_numerical_values():
    with pytest.raises(ValidationError):
        BusinessReport(metric_ids=["order_count"], order_count=3)


def test_same_production_agent_renders_metrics_from_references_without_numeric_prose(source):
    _, adapter = source
    runner = AgenticRunner()
    runner._agent = runner._build_agent()
    step, observed = 0, []

    def model(messages, info):
        nonlocal step
        step += 1
        if step == 1:
            assert not info.output_tools
            return ModelResponse(
                [ToolCallPart("query_business_metrics", {"metric_ids": list(DEFINITIONS)})]
            )
        assert info.output_tools
        assert not info.function_tools  # 证据齐全后进入报告阶段。
        for message in messages:
            observed.extend(
                part.content
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "query_business_metrics"
            )
        if step == 2:
            # 模型不能通过写出一个看似合理、却属于其他业务粒度的数字，
            # 来绕过只引用结果的报告。
            return ModelResponse([TextPart("订单数3，总金额200。")])
        return ModelResponse(
            [ToolCallPart("complete_business_report", {"metric_ids": list(DEFINITIONS)})]
        )

    evaluation = EvaluationRun(
        guidance=DEFAULT_GUIDANCE,
        params={},
        authorized_tenants=("a",),
        business_adapter=adapter,
        business_metrics=DEFINITIONS,
        business_schema="Registered metrics: order_count,order_total,item_count.",
    )

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "查询数据库订单数和总金额，以及商品行数",
                UserContext(user_id="metrics-test", email="metrics@local.test", tenant_id="a"),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert evaluation.status == "ok"
    # 所需证据已齐全。忽略错误的数字描述，
    # 直接渲染实际结果，不再多走一轮模型补全。
    assert step == 2
    assert "订单数3" not in answer
    assert "| 订单数 | 2 | orders | order_count |" in answer
    assert "| 商品行数 | 3 | items | item_count |" in answer
    assert "| 订单总金额 | 200.0 | 未定义 | order_total |" in answer
    assert len({m["snapshot_id"] for m in observed[-1]["metrics"]}) == 1


def test_production_agent_cannot_finish_with_wrong_entity_count_and_unjoined_details(source):
    """原有的自然语言遗漏必须在类型化修复之前就失败。"""
    _, adapter = source
    runner = AgenticRunner()
    runner._agent = runner._build_agent()
    step = 0

    def model(messages, info):
        nonlocal step
        step += 1
        if step == 1:
            return ModelResponse(
                [
                    ToolCallPart("query_business_metrics", {"metric_ids": ["item_count"]}),
                    ToolCallPart(
                        "query_database", {"sql": "SELECT item_id,order_id,price FROM items"}
                    ),
                ]
            )
        if step == 2:
            assert not info.output_tools
            return ModelResponse([TextPart("已完成全部明细和统计。")])
        if step == 3:
            feedback = " ".join(
                str(part.content)
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart) and "<retry_feedback>" in str(part.content)
            )
            assert "metric:order_count" in feedback
            assert "metric:order_total" in feedback
            assert "db_join:items,orders" in feedback
            assert "metric:item_count" not in feedback
            return ModelResponse(
                [
                    ToolCallPart(
                        "query_business_metrics", {"metric_ids": ["order_count", "order_total"]}
                    ),
                    ToolCallPart(
                        "query_database",
                        {
                            "sql": "SELECT i.item_id,i.order_id,i.price,o.amount FROM items i JOIN orders o ON i.order_id=o.order_id"
                        },
                    ),
                ]
            )
        assert info.output_tools
        assert not info.function_tools
        return ModelResponse([TextPart("完成。")])

    evaluation = EvaluationRun(
        guidance=DEFAULT_GUIDANCE,
        params={},
        authorized_tenants=("a",),
        business_adapter=adapter,
        business_metrics=DEFINITIONS,
        business_schema="Registered metrics: order_count,order_total,item_count.",
    )

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "请关联商品与订单列出完整明细，并使用已登记业务指标给出订单数、订单总金额和商品行数。",
                UserContext(user_id="contract-test", email="contract@local.test", tenant_id="a"),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert evaluation.status == "ok"
    assert step == 4
    assert "| 订单数 | 2 | orders | order_count |" in answer
    assert "| 商品行数 | 3 | items | item_count |" in answer
    assert "| 订单总金额 | 200.0 | 未定义 | order_total |" in answer


def test_catalog_names_aliases_and_negative_requests_bind_exact_obligations():
    from src.agent.business_contract import catalog_requirements

    definitions = {
        **DEFINITIONS,
        "order_count": {**DEFINITIONS["order_count"], "aliases": ["成交笔数"]},
    }
    assert catalog_requirements(
        "查数据库成交笔数和商品行数，不要查订单总金额。", definitions, {"db", "db_count"}
    ) == {"metric:order_count", "metric:item_count"}
    assert catalog_requirements("订单数的口径是什么？", definitions, set()) == set()
    assert catalog_requirements("查数据库order_count_extra", definitions, {"db"}) == set()


def test_join_detail_and_selected_report_cannot_omit_requested_amount(source):
    from src.agent.business_contract import catalog_requirements
    from src.agent.grounding import EvidenceLedger
    from src.agent.metric_results import render_business_body

    _, adapter = source
    ledger = EvidenceLedger(required={"db", "db_details"})
    ledger.required |= catalog_requirements(
        "关联订单与商品明细，查询订单数、订单总金额和商品行数。", DEFINITIONS, ledger.required
    )
    assert "db_field:orders.amount" in ledger.required
    session = BusinessReadSession()
    try:
        for measures, sql in metric_queries(DEFINITIONS, list(DEFINITIONS), adapter.profile.views):
            result, metrics = execute_metric_group(session, "a", measures, sql, adapter=adapter)
            identifier = ledger.add(
                "db", result["rows"], sql=sql, query_semantics=result["query_semantics"]
            )
            for metric in metrics:
                ledger.add("metric", metric, query_evidence_id=identifier)

        def detail(sql):
            result = session.read(sql, "a", adapter=adapter)
            return ledger.add(
                "db", result["rows"], sql=sql, query_semantics=result["query_semantics"]
            )

        incomplete = detail(
            "SELECT i.item_id,i.order_id,i.price FROM items i JOIN orders o ON i.order_id=o.order_id"
        )
        assert "db_field:orders.amount" in ledger.missing()
        assert "db_join:items,orders" in ledger.missing()
        detail(
            "SELECT i.item_id,i.order_id,i.price,o.amount AS 订单金额 FROM items i JOIN orders o ON i.order_id=o.order_id"
        )
        assert not ledger.missing()
        # 台账中其他位置的完整证据，不能让报告中
        # 明确选定的不完整结果变得合法。
        with pytest.raises(ValueError, match="报告选定的明细"):
            render_business_body(
                BusinessReport(metric_ids=list(DEFINITIONS), detail_evidence_ids=[incomplete]),
                ledger,
            )
    finally:
        session.close()


def test_constant_or_computed_alias_cannot_impersonate_source_amount(source):
    from src.agent.business_contract import completed_catalog_requirements

    _, adapter = source
    session = BusinessReadSession()
    try:
        for expression in ("0 AS amount", "o.amount*0 AS amount"):
            sql = f"SELECT i.item_id,i.order_id,{expression} FROM items i JOIN orders o ON i.order_id=o.order_id"
            result = session.read(sql, "a", adapter=adapter)
            entry = dict(
                kind="db", data=result["rows"], sql=sql, query_semantics=result["query_semantics"]
            )
            required = {"db_field:orders.amount", "db_join:items,orders"}
            assert not required & completed_catalog_requirements([entry], required)
    finally:
        session.close()


def test_metric_correction_feedback_never_drops_filters_or_exposes_catalog_after_denial(source):
    from src.agent.business_session import business_read_session
    from src.agent.dependencies import AgentDeps
    from src.agent.metric_tools import execute_business_metrics

    _, adapter = source
    deps = AgentDeps(
        context=UserContext(user_id="feedback-test", email="feedback@local.test", tenant_id="a"),
        evaluation=EvaluationRun(
            guidance=DEFAULT_GUIDANCE,
            params={},
            authorized_tenants=("a",),
            business_adapter=adapter,
            business_metrics=DEFINITIONS,
        ),
    )

    async def run():
        async with business_read_session() as session:
            unknown = await execute_business_metrics(deps, ["guessed_order_metric"])
            assert unknown["available_metric_ids"] == sorted(DEFINITIONS)
            invalid_filter = await execute_business_metrics(
                deps,
                ["order_count", "item_count"],
                [MetricFilter(field="amount", op="gt", value=100)],
            )
            assert invalid_filter["common_filter_fields"] == ["order_id"]
            assert not deps.ledger.entries and session.snapshot is None
            deps.context.tenant_id = "b"
            denied = await execute_business_metrics(deps, ["guessed_order_metric"])
            assert "error" in denied and "available_metric_ids" not in denied
            assert not deps.ledger.entries and session.snapshot is None

    asyncio.run(run())


def test_actual_agent_rejects_unrequested_filter_before_opening_source(source):
    from src.agent.business_session import current_business_session

    _, adapter = source
    phase = 0

    def model(messages, info):
        nonlocal phase
        phase += 1
        if phase == 1:
            return ModelResponse(
                [
                    ToolCallPart(
                        "query_database", {"sql": "SELECT SUM(amount) FROM orders WHERE amount > 0"}
                    )
                ]
            )
        if phase == 2:
            results = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            assert "未执行查询" in str(results[-1].content)
            assert current_business_session().snapshot is None
            return ModelResponse(
                [
                    ToolCallPart(
                        "query_database", {"sql": "SELECT SUM(amount) AS total_amount FROM orders"}
                    )
                ]
            )
        return ModelResponse([TextPart("总金额：200。")])

    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))
    evaluation = EvaluationRun(
        guidance=DEFAULT_GUIDANCE,
        params={},
        authorized_tenants=("a",),
        business_adapter=adapter,
        business_schema="orders(order_id, amount); no registered metrics",
    )

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "查询全部订单总金额",
                UserContext(user_id="filter-basis", email="filter@local.test", tenant_id="a"),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert "200" in answer
    assert evaluation.status == "ok"
