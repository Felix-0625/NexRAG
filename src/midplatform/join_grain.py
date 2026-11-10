"""Certify fact-to-dimension joins with snapshot-enforced keys.

Require a fact root and complete unique dimension keys. Reject sums or averages over
repeated dimensions even if fact rows survive. Unsupported cardinalities require separate
queries in the same snapshot, never silent acceptance or SUM(DISTINCT amount).
"""

from dataclasses import dataclass

from sqlglot import exp


@dataclass
class JoinProof:
    """Certified join aliases, keys, and row-grain constraints."""

    root_alias: str
    root_name: str
    aliases: dict
    unique_aliases: set

    def identity(self, column):
        """Resolve columns to aliases and lowercase names, rejecting unknown or ambiguous
        identities.
        """
        if column.table:
            alias = column.table.lower()
            if alias not in self.aliases:
                raise ValueError("关联列引用了未知业务范围")
        else:
            matches = [
                name
                for name, view in self.aliases.items()
                if column.name.lower() in {c.lower() for c in view.columns}
            ]
            if len(matches) != 1:
                raise ValueError("关联中的列不明确，请使用表别名限定列名")
            alias = matches[0]
        return alias, column.name.lower()


def _equalities(node):
    if isinstance(node, exp.And):
        return _equalities(node.left) + _equalities(node.right)
    return [node] if isinstance(node, exp.EQ) else []


def _fail(message):
    """Reject uncertified joins with a consistent repair hint."""
    from src.midplatform.query_semantics import QuerySemanticsError

    raise QuerySemanticsError(
        message + "；请以事实表为起点关联唯一键，或在同一轮快照中分别查询后汇总"
    )


def _root_proof(query, lookup):
    """Certify the FROM fact table and initialize its proof."""
    root = query.args.get("from_")
    root = root.this if root else None
    if not isinstance(root, exp.Table) or root.name.lower() not in lookup:
        _fail("无法认证关联起点的业务粒度")
    return JoinProof(
        root.alias_or_name.lower(),
        root.name,
        {root.alias_or_name.lower(): lookup[root.name.lower()]},
        {root.alias_or_name.lower()},
    )


def _join_target(proof, lookup, join):
    """Validate a JOIN target, join type, and complete unique key."""
    table = join.this
    if not isinstance(table, exp.Table) or table.name.lower() not in lookup:
        _fail("关联目标缺少可认证的业务唯一键")
    if (
        join.side.upper() not in {"", "LEFT"}
        or join.kind.upper() in {"CROSS", "SEMI", "ANTI"}
        or join.args.get("method")
    ):
        _fail("当前关联类型不能认证事实行粒度")
    alias = table.alias_or_name.lower()
    if alias in proof.aliases:
        _fail("关联表别名必须唯一")
    view = lookup[table.name.lower()]
    keys = {key.lower() for key in view.primary_key}
    if not keys:
        _fail("关联目标未声明并强制业务唯一键")
    return alias, view, keys


def prove_join_grain(query, views):
    """Certify row-preserving joins or raise QuerySemanticsError.

    Require a registered fact root and LEFT JOINs covering target keys; reject aggregation
    over repeated joined values.
    """
    lookup = {name.lower(): view for name, view in views.items()}
    proof = _root_proof(query, lookup)
    for join in query.args.get("joins", []):
        alias, view, keys = _join_target(proof, lookup, join)
        left_aliases = set(proof.aliases)
        proof.aliases[alias] = view
        pairs = _join_pairs(proof, join, alias, left_aliases)
        if not keys <= {right[1] for right, left in pairs}:
            _fail("关联可能使事实行重复：ON/USING 没有覆盖目标完整唯一键")
        _mark_unique(proof, alias, left_aliases, pairs)
    _check_aggregates(query, proof)
    return proof


def _join_pairs(proof, join, alias, left_aliases):
    """Collect right-left equality pairs from USING or ON conditions."""
    pairs = []
    # QuerySemanticsError inherits ValueError; preserve the existing behavior where
    # the handler catches _fail and appends its hint again.
    try:
        if join.args.get("using"):
            for column in join.args["using"]:
                name = column.name.lower()
                matches = [
                    a for a in left_aliases if name in {c.lower() for c in proof.aliases[a].columns}
                ]
                if len(matches) != 1:
                    _fail("USING 无法唯一定位左侧字段")
                pairs.append(((alias, name), (matches[0], name)))
        else:
            for condition in _equalities(join.args.get("on")):
                if not isinstance(condition.left, exp.Column) or not isinstance(
                    condition.right, exp.Column
                ):
                    continue
                one, two = proof.identity(condition.left), proof.identity(condition.right)
                if one[0] != alias:
                    one, two = two, one
                if one[0] == alias and two[0] in left_aliases:
                    pairs.append((one, two))
    except ValueError as exc:
        _fail(str(exc))
    return pairs


def _mark_unique(proof, alias, left_aliases, pairs):
    """Check whether a newly joined table stays unique within fact rows."""
    # Dimension rows can repeat across facts; uniqueness requires equalities
    # covering a previously unique left-side key too.
    for left_alias in left_aliases & proof.unique_aliases:
        left_keys = {key.lower() for key in proof.aliases[left_alias].primary_key}
        if left_keys and left_keys <= {left[1] for right, left in pairs if left[0] == left_alias}:
            proof.unique_aliases.add(alias)


def _check_aggregates(query, proof):
    """Reject aggregates over numeric fields in repeated joined rows."""
    for aggregate in query.find_all(exp.Func):
        if not isinstance(aggregate, exp.AggFunc) and not (
            isinstance(aggregate, exp.Anonymous) and aggregate.name.upper() == "TOTAL"
        ):
            continue
        # Nested SELECT aggregates are outside this proof's certification scope.
        if aggregate.find_ancestor(exp.Select) is not query:
            continue
        if isinstance(aggregate, (exp.Min, exp.Max)):
            continue
        if isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct):
            continue
        try:
            owners = {proof.identity(column)[0] for column in aggregate.find_all(exp.Column)}
        except ValueError as exc:
            _fail(str(exc))
        if owners - proof.unique_aliases:
            _fail("汇总引用会在事实行中重复的关联表数值，可能重复计数或加权")


def free_column_nodes(node):
    """Collect columns outside aggregate and window expressions."""
    if isinstance(node, (exp.AggFunc, exp.Window)) or (
        isinstance(node, exp.Anonymous) and node.name.upper() == "TOTAL"
    ):
        return []
    if isinstance(node, exp.Column):
        return [node]
    return [column for child in node.iter_expressions() for column in free_column_nodes(child)]
