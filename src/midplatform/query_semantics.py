"""用 SQLGlot AST 和私有快照中强制执行的键检查查询粒度。

这有意设计为有界的语义检查，而不是对任意 SQL 的证明。复杂连接/子查询仍不予认证，
写保护和租户防护照常生效。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import sqlglot
from sqlglot import exp


class QuerySemanticsError(ValueError):
    """查询的聚合投影存在歧义。"""


@dataclass(frozen=True)
class QueryShape:
    row_preserving: bool = False
    grouped: bool = False
    aggregate: bool = False
    limited: bool = False
    window_fields: tuple[str, ...] = ()
    grain: str | None = None
    join_cardinality: str | None = None
    # 仅用于已认证的连接：可安全用于确定性汇总的输出字段。
    # None 保留旧的单视图证据；空元组表示一个都不允许。
    sum_fields: tuple[str, ...] | None = None
    count_fields: tuple[str, ...] | None = None
    distinct_order_keys: tuple[tuple[str, ...], ...] = ()
    warning: str | None = None

    def as_dict(self):
        """转换为普通字典。"""
        return asdict(self)


def _free_columns(node):
    if isinstance(node, (exp.AggFunc, exp.Window)) or (
        isinstance(node, exp.Anonymous) and node.name.upper() == "TOTAL"
    ):
        return []
    if isinstance(node, exp.Column):
        return [node.name.lower()]
    return [column for child in node.iter_expressions() for column in _free_columns(child)]


def _collapses_rows(node):
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Window):
            return False
        if isinstance(parent, (exp.AggFunc, exp.Select)):
            return True
        parent = parent.parent
    return True


def _prove_joins(query, views):
    """逐个 SELECT 验证连接粒度，只返回最外层查询的证明。"""
    from src.midplatform.join_grain import prove_join_grain

    join_proof = None
    if views is not None:
        # 嵌套的连接也要逐个校验。外层 CTE 不能把 fanout 聚合藏在内层 SELECT 里，
        # 即使其输出本身未被认证。
        for select in query.find_all(exp.Select):
            if select.args.get("joins"):
                proof = prove_join_grain(select, views)
                if select is query:
                    join_proof = proof
    return join_proof


def _resolve_groups(group, projections, aliases):
    """把 GROUP BY 中的位置序号和别名还原为实际表达式。"""
    groups = []
    for item in group.expressions if group else []:
        if isinstance(item, exp.Literal) and item.is_int:
            index = int(item.this) - 1
            if not 0 <= index < len(projections):
                raise QuerySemanticsError("分组位置超出查询列范围")
            item = projections[index]
        elif isinstance(item, exp.Column) and item.name.lower() in aliases:
            item = aliases[item.name.lower()]
        groups.append(item)
    return groups


def _collapsing_aggregates(query):
    # OVER 之下的聚合属于窗口函数，不会折叠行。
    return [
        node
        for node in query.find_all(exp.Func)
        if (
            isinstance(node, exp.AggFunc)
            or isinstance(node, exp.Anonymous)
            and node.name.upper() == "TOTAL"
        )
        and _collapses_rows(node)
    ]


def _root_view(join_proof, views, tables):
    """取得根视图：有连接证明时用其根别名，否则按表名匹配。"""
    if join_proof:
        return join_proof.aliases[join_proof.root_alias]
    return next(
        (view for name, view in (views or {}).items() if name.lower() == tables[0].name.lower()),
        None,
    )


def _enforced_keys(view, join_proof):
    return {
        (join_proof.root_alias, key.lower()) if join_proof else key.lower()
        for key in getattr(view, "primary_key", ())
    }


def _check_aggregate_projections(
    projections, grouped_expressions, grouped_columns, key_grouped, join_proof, identity
):
    """聚合/分组查询中，拒绝 SELECT * 和未分组且无唯一键依赖的明细列。"""
    from src.midplatform.join_grain import free_column_nodes

    for item in projections:
        if isinstance(item, exp.Star):
            raise QuerySemanticsError("聚合中的 SELECT * 无法验证字段粒度，请明确列名")
        if item.sql(dialect="sqlite", normalize=True) in grouped_expressions:
            continue
        free_columns = (
            {identity(column) for column in free_column_nodes(item)}
            if join_proof
            else set(_free_columns(item))
        )
        if not key_grouped and free_columns - grouped_columns:
            raise QuerySemanticsError(
                "聚合包含未分组且没有唯一键依赖的明细列；请明确分组粒度或分别查询明细和汇总"
            )


def _preserves_rows(query, group, aggregates, key_grouped, keys, output_columns):
    preserves = not aggregates and group is None
    if group is not None:
        preserves = key_grouped and keys <= output_columns
    return (
        preserves
        and (not query.args.get("distinct") or bool(keys) and keys <= output_columns)
        and not query.args.get("having")
    )


def _distinct_order_keys(query, projections, join_proof, preserves, identity):
    """订单粒度视图的唯一键若全部出现在输出列中，返回对应的输出字段名。"""
    distinct_order_keys = []
    if join_proof and preserves and not query.args.get("limit") and not query.args.get("offset"):
        for alias, joined_view in join_proof.aliases.items():
            if joined_view.grain not in {"order", "orders"} or not joined_view.primary_key:
                continue
            outputs = {
                identity(item): output.alias_or_name
                for output, item in zip(query.expressions, projections)
                if isinstance(item, exp.Column)
            }
            key = [(alias, name.lower()) for name in joined_view.primary_key]
            if all(column in outputs for column in key):
                distinct_order_keys.append(tuple(outputs[column] for column in key))
    return distinct_order_keys


def _sum_fields(query, projections, join_proof, preserves, identity):
    """可安全做确定性求和的输出字段；单视图明细证据保持 None。"""
    if not (join_proof or not preserves):
        return None
    return tuple(
        output.alias_or_name
        for output, item in zip(query.expressions, projections)
        if not item.find(exp.Window)
        and (
            isinstance(item, exp.Sum)
            or isinstance(item, exp.Anonymous)
            and item.name.upper() == "TOTAL"
            or isinstance(item, exp.Column)
            and preserves
            and (not join_proof or identity(item)[0] in join_proof.unique_aliases)
        )
    )


def _count_fields(query, projections):
    return tuple(
        output.alias_or_name
        for output, item in zip(query.expressions, projections)
        if isinstance(item, exp.Count) and not item.find(exp.Window)
    )


def _shape_warning(group, preserves, aggregates):
    if group and not preserves:
        return "分组未能证明保留业务唯一键，不能认证为明细"
    if preserves or aggregates:
        return None
    return "结果粒度未认证，请核对业务唯一键"


def inspect_query(sql: str, views=None) -> QueryShape:
    """认证简单的行粒度；拒绝 SQLite 中结果不确定的裸列。

    分组查询只有在分组和输出中都包含已强制的主键时，才保留源行。仅用窗口函数
    本身就保留行，不能误判为折叠行的聚合。
    """
    query = sqlglot.parse_one(sql, read="sqlite")
    if not isinstance(query, exp.Select):
        return QueryShape(warning="查询不是可验证的 SELECT")
    join_proof = _prove_joins(query, views)
    if (
        query.args.get("with_")
        or query.find(exp.Subquery)
        or (query.args.get("joins") and join_proof is None)
    ):
        return QueryShape(warning="复杂关联/子查询的粒度尚未认证，不能直接当作完整明细证明")
    tables = list(query.find_all(exp.Table))
    if len(tables) != 1 and join_proof is None:
        return QueryShape(warning="需要明确业务视图才能验证结果粒度")
    projections = [item.this if isinstance(item, exp.Alias) else item for item in query.expressions]
    aliases = {item.alias.lower(): item.this for item in query.expressions if item.alias}
    group = query.args.get("group")
    groups = _resolve_groups(group, projections, aliases)
    identity = join_proof.identity if join_proof else lambda column: column.name.lower()
    grouped_columns = {identity(item) for item in groups if isinstance(item, exp.Column)}
    grouped_expressions = {item.sql(dialect="sqlite", normalize=True) for item in groups}
    aggregates = _collapsing_aggregates(query)
    view = _root_view(join_proof, views, tables)
    keys = _enforced_keys(view, join_proof)
    key_grouped = bool(keys) and keys <= grouped_columns
    output_columns = {identity(item) for item in projections if isinstance(item, exp.Column)}
    if aggregates or group:
        _check_aggregate_projections(
            projections, grouped_expressions, grouped_columns, key_grouped, join_proof, identity
        )
    window_fields = tuple(
        projection.alias_or_name for projection in query.expressions if projection.find(exp.Window)
    )
    preserves = _preserves_rows(query, group, aggregates, key_grouped, keys, output_columns)
    distinct_order_keys = _distinct_order_keys(query, projections, join_proof, preserves, identity)
    return QueryShape(
        row_preserving=bool(preserves),
        grouped=group is not None,
        aggregate=bool(aggregates),
        limited=bool(query.args.get("limit") or query.args.get("offset")),
        window_fields=window_fields,
        grain=getattr(view, "grain", None) if preserves and keys else None,
        join_cardinality="fact_to_unique_lookup" if join_proof else None,
        sum_fields=_sum_fields(query, projections, join_proof, preserves, identity),
        count_fields=_count_fields(query, projections),
        distinct_order_keys=tuple(distinct_order_keys),
        warning=_shape_warning(group, preserves, aggregates),
    )


def evidence_shape(entry) -> dict:
    """没有有效查询的旧版/评测证据无法认证粒度。"""
    if entry.get("query_semantics") is not None:
        return entry["query_semantics"]
    try:
        return inspect_query(entry.get("sql", "")).as_dict()
    except (ValueError, sqlglot.errors.ParseError):
        return QueryShape(warning="缺少可认证的查询结构").as_dict()
