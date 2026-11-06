"""面向现有单 Agent 和授权路径的已注册指标工具。"""

import asyncio
import sqlite3

from pydantic_ai import RunContext

from src.agent.authorization import require_business
from src.agent.business_session import current_business_session
from src.agent.dependencies import AgentDeps
from src.config import settings
from src.midplatform.catalog import MetadataCatalog
from src.midplatform.metric_query import MetricFilter, execute_metric_group, metric_queries


def register_metric_tools(agent):
    @agent.tool(strict=False)
    async def query_business_metrics(
        ctx: RunContext[AgentDeps], metric_ids: list[str], filters: list[MetricFilter] | None = None
    ) -> dict:
        """Query registered business metrics by ID, not free-form SQL or guessed totals.

        Use catalog metric IDs from the business schema. Filters are explicit
        field comparisons and apply to every requested metric. All groups share
        one authorized snapshot. Unregistered exploration uses query_database.
        """
        return await execute_business_metrics(ctx.deps, metric_ids, filters)


def _resolve_metric_source(deps, session):
    """取得指标查询所用的只读适配器与指标定义（评测运行优先，否则读取当前连接的元数据）。"""
    evaluation = deps.evaluation
    if evaluation is not None:
        adapter = evaluation.business_adapter
        definitions = evaluation.business_metrics or {}
    else:
        from src.midplatform.adapters import create_business_adapter
        from src.midplatform.workspace import active_connection

        active = active_connection()
        adapter = session.adapter or create_business_adapter()
        catalog = MetadataCatalog(active["metadata_path"] if active else settings.METADATA_PATH)
        definitions = catalog.metadata.get("metrics", {})
    if adapter is None:
        raise ValueError("业务指标需要真实的只读适配器")
    return adapter, definitions


def _common_filter_fields(adapter, definitions, metric_ids):
    """所请求指标所在业务视图的公共字段；任一视图不在适配器中时返回空集合。"""
    tables = {definitions[name].get("table") for name in metric_ids}
    if not tables or not tables <= set(adapter.profile.views):
        return set()
    return set.intersection(
        *(
            set(adapter.profile.views[table].columns)
            for table in tables
            if table in adapter.profile.views
        )
    )


async def _run_metric_queries(deps, session, adapter, queries, conditions):
    """逐组执行指标查询：每组前重新授权，并把查询与指标值记入证据台账。"""
    metrics, evidence_ids = [], []
    for measures, sql in queries:
        require_business(deps.context, deps.evaluation)
        result, values = await asyncio.to_thread(
            execute_metric_group,
            session,
            deps.context.tenant_id,
            measures,
            sql,
            adapter=adapter,
            filters=conditions,
        )
        evidence_id = deps.ledger.add(
            "db",
            result["rows"],
            sql=sql,
            query_semantics=result["query_semantics"],
            snapshot_id=result["snapshot_id"],
        )
        evidence_ids.append(evidence_id)
        for value in values:
            deps.ledger.add("metric", value, query_evidence_id=evidence_id)
        metrics.extend(values)
        deps.evidence_calls += 1
    return metrics, evidence_ids


def _metric_response(deps, session, metrics, evidence_ids):
    """组装返回给 Agent 的指标结果及后续动作提示。"""
    return {
        "metrics": metrics,
        "metric_query_evidence_ids": evidence_ids,
        "detail_evidence_ids": [],
        "remaining_evidence": sorted(deps.ledger.missing()),
        "next_action": (
            "query_database: run a plain detail SELECT; metric query IDs are aggregate evidence, not detail rows."
            if "db_details" in deps.ledger.missing()
            else "Gather remaining required evidence, then complete_business_report using actual IDs."
        ),
        "result_grain": "registered_metric",
        "snapshot_id": session.snapshot.id,
        "note": "Metric values are business measures, not query result row counts. Units are only those in the catalog.",
    }


async def execute_business_metrics(deps, metric_ids, filters=None):
    """供 Agent 调用和已绑定的目录义务共用的、带授权的执行器。"""
    try:
        require_business(deps.context, deps.evaluation)
        session = current_business_session()
        if session is None:
            raise ValueError("没有当前只读业务会话")
        adapter, definitions = _resolve_metric_source(deps, session)
        conditions = filters or []
        unknown = set(metric_ids) - set(definitions)
        if unknown:
            return {
                "error": "指标ID未注册，请使用业务目录中的确切ID；没有执行查询",
                "available_metric_ids": sorted(definitions),
                "remaining_evidence": sorted(deps.ledger.missing()),
            }
        common_fields = _common_filter_fields(adapter, definitions, metric_ids)
        if any(condition.field not in common_fields for condition in conditions):
            return {
                "error": "过滤字段不适用于全部请求指标；没有执行查询，也没有丢弃过滤条件。请按业务视图拆分指标调用并保留请求的过滤条件",
                "common_filter_fields": sorted(common_fields),
                "metric_views": {name: definitions[name].get("table") for name in metric_ids},
                "remaining_evidence": sorted(deps.ledger.missing()),
            }
        from src.agent.query_basis import require_filter_basis

        for condition in conditions:
            require_filter_basis(condition.value, deps.ledger.prompt)
        queries = metric_queries(definitions, metric_ids, adapter.profile.views, conditions)
        metrics, evidence_ids = await _run_metric_queries(
            deps, session, adapter, queries, conditions
        )
        return _metric_response(deps, session, metrics, evidence_ids)
    except (ValueError, FileNotFoundError, ConnectionError, TimeoutError, sqlite3.Error):
        return {
            "error": "指标未执行或配置无效，请核对已注册指标、过滤条件和当前授权；没有返回猜测数值"
        }
