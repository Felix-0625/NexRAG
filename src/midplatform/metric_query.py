"""Execute operator-defined metrics through SQLGlot and read-only snapshots.

This bounded catalog compiler is not a general optimizer. Aggregates belong to one declared
view; cross-view requests run separately within the same snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from src.midplatform.join_grain import free_column_nodes


class MetricDefinition(BaseModel):
    """Operator-defined metric expression tied to a declared business view."""

    model_config = ConfigDict(extra="forbid")
    table: str
    calculation: str
    description: str
    aliases: list[str] = Field(default_factory=list, max_length=20)
    kind: Literal["count", "amount", "number"] = "number"
    unit: str | None = None


class MetricFilter(BaseModel):
    """Validated field predicate applied to registered metric queries."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    field: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte"] = "eq"
    value: str | int | float


class MetricValue(BaseModel):
    """Typed metric result with query and snapshot provenance."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    metric_id: str
    label: str
    value: Decimal | None
    kind: Literal["count", "amount", "number"]
    unit: str | None
    view: str
    grain: str | None
    calculation: str
    scope_id: str
    snapshot_id: str
    filters: list[MetricFilter]


def _expression(spec, views):
    if spec.table not in views:
        raise ValueError("指标引用了未授权的业务视图")
    expressions = sqlglot.parse(spec.calculation, read="sqlite")
    if len(expressions) != 1 or expressions[0] is None:
        raise ValueError("指标定义必须只包含一个表达式")
    node = expressions[0]
    if (
        node.find(exp.Select, exp.Subquery, exp.Table, exp.Window)
        or not node.find(exp.AggFunc)
        or free_column_nodes(node)
    ):
        raise ValueError("指标必须是单视图聚合表达式，不包含明细裸列、窗口或子查询")
    for column in node.find_all(exp.Column):
        if column.table not in {"", spec.table} or column.name not in views[spec.table].columns:
            raise ValueError("指标字段与声明的业务视图不符")
    if spec.kind == "count" and not isinstance(node, exp.Count):
        raise ValueError("计数指标必须使用 COUNT 或 COUNT DISTINCT")
    return node


def metric_queries(definitions, names, views, filters=()):
    """Compile a small metric group, rejecting unknown IDs and filters first."""
    if not names or len(names) > 10 or len(filters) > 20:
        raise ValueError("每批需要1–10个指标，最多20个过滤条件")
    groups = defaultdict(list)
    for name in dict.fromkeys(names):
        if name not in definitions:
            raise ValueError("指标未注册，请选择业务目录中的指标；探索查询请使用只读SQL工具")
        spec = MetricDefinition.model_validate(definitions[name])
        node = _expression(spec, views)
        groups[spec.table].append((name, spec, node))
    compiled = []
    operators = {
        "eq": exp.EQ,
        "ne": exp.NEQ,
        "gt": exp.GT,
        "gte": exp.GTE,
        "lt": exp.LT,
        "lte": exp.LTE,
    }
    for table, measures in groups.items():
        query = exp.select(
            *[node.copy().as_(f"metric_{index}") for index, (_, _, node) in enumerate(measures)]
        ).from_(exp.to_table(table))
        conditions = []
        for condition in filters:
            if condition.field not in views[table].columns:
                raise ValueError("过滤字段不适用于全部请求指标；没有静默丢弃过滤条件")
            conditions.append(
                operators[condition.op](
                    this=exp.column(condition.field), expression=exp.convert(condition.value)
                )
            )
        if conditions:
            query = query.where(exp.and_(*conditions))
        compiled.append((measures, query.sql(dialect="sqlite")))
    return compiled


def execute_metric_group(session, tenant_id, measures, sql, *, adapter=None, filters=()):
    """Keep the SQL tool's actual snapshot as the sole source of metric values."""
    result = session.read(sql, tenant_id, adapter=adapter, max_rows=1)
    if len(result["rows"]) != 1:
        raise ValueError("指标查询必须返回一个聚合结果，不能用明细行数替代")
    values = []
    for index, (name, spec, _) in enumerate(measures):
        value = result["rows"][0][f"metric_{index}"]
        metric = MetricValue(
            metric_id=name,
            label=spec.description,
            value=value,
            kind=spec.kind,
            unit=spec.unit,
            view=spec.table,
            grain=session.snapshot.views[spec.table].grain,
            calculation=spec.calculation,
            scope_id=tenant_id,
            snapshot_id=result["snapshot_id"],
            filters=list(filters),
        )
        if (
            metric.kind == "count"
            and metric.value is not None
            and (metric.value < 0 or metric.value != metric.value.to_integral_value())
        ):
            raise ValueError("计数指标返回了无效值")
        values.append(metric.model_dump(mode="json"))
    return result, values
