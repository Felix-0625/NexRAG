"""Check query grain with SQLGlot and snapshot-enforced keys.

This bounded check is not proof for arbitrary SQL. Reject unsupported complex joins and
subqueries while keeping write and tenant guards independent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import sqlglot
from sqlglot import exp


class QuerySemanticsError(ValueError):
    """Report ambiguous aggregate projections."""


@dataclass(frozen=True)
class QueryShape:
    """Certified output grain and safe deterministic aggregation fields."""

    row_preserving: bool = False
    grouped: bool = False
    aggregate: bool = False
    limited: bool = False
    window_fields: tuple[str, ...] = ()
    grain: str | None = None
    join_cardinality: str | None = None
    # Only certified joins expose fields safe for deterministic aggregation.
    # None preserves legacy single-view evidence; an empty tuple permits nothing.
    sum_fields: tuple[str, ...] | None = None
    count_fields: tuple[str, ...] | None = None
    distinct_order_keys: tuple[tuple[str, ...], ...] = ()
    warning: str | None = None

    def as_dict(self):
        """Convert query shape to a plain dictionary."""
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
    """Validate all SELECT joins and return only the outermost proof."""
    from src.midplatform.join_grain import prove_join_grain

    join_proof = None
    if views is not None:
        # Validate nested joins too: outer CTEs must not hide inner fanout aggregates,
        # even if the outer output is itself uncertified.
        for select in query.find_all(exp.Select):
            if select.args.get("joins"):
                proof = prove_join_grain(select, views)
                if select is query:
                    join_proof = proof
    return join_proof


def _resolve_groups(group, projections, aliases):
    """Resolve GROUP BY positions and aliases into expressions."""
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
    # Aggregates under OVER are windows and do not collapse rows.
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
    """Find the root from a join proof or table name."""
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
    """Reject stars and ungrouped details lacking unique-key dependence."""
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
    """Return projected order-key fields only when the full unique key appears."""
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
    """Identify safely summable fields, preserving None for single-view legacy details."""
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
    """Certify simple row grain and reject SQLite's indeterminate bare columns.

    Groups retain source rows only when enforced keys appear in grouping and output.
    Window-only queries retain rows and are not collapsing aggregates.
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
    """Leave legacy or evaluation evidence without valid SQL uncertified."""
    if entry.get("query_semantics") is not None:
        return entry["query_semantics"]
    try:
        return inspect_query(entry.get("sql", "")).as_dict()
    except (ValueError, sqlglot.errors.ParseError):
        return QueryShape(warning="缺少可认证的查询结构").as_dict()
