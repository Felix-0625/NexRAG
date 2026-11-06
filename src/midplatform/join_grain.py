"""依据租户快照中强制执行的键，认证事实表到维表的连接。

这是小型系统中语义层 fanout 防护的等价实现：查询锚定在事实粒度，且只能用维表
完整的唯一键连接。重复的维表值参与 SUM/AVG 会被拒绝，即便连接本身保留了事实行。
不支持的基数关系必须在同一轮快照内分别查询，不能悄悄放行，也不能用
SUM(DISTINCT amount) 掩盖。
"""

from dataclasses import dataclass

from sqlglot import exp


@dataclass
class JoinProof:
    root_alias: str
    root_name: str
    aliases: dict
    unique_aliases: set

    def identity(self, column):
        """把列解析为 (表别名, 小写列名)；别名未知或未限定的列名在多个表中重名时抛出 ValueError。"""
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
    """以统一的修复提示拒绝无法认证的关联。"""
    from src.midplatform.query_semantics import QuerySemanticsError

    raise QuerySemanticsError(
        message + "；请以事实表为起点关联唯一键，或在同一轮快照中分别查询后汇总"
    )


def _root_proof(query, lookup):
    """认证关联起点（FROM 的事实表），并建立初始证明。"""
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
    """校验单个 JOIN 的目标表与连接类型，返回别名、视图和完整唯一键。"""
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
    """认证一个带 JOIN 的 SELECT 是否保持事实表行粒度，返回 JoinProof。

    起点须为已登记的事实表，其余只允许 LEFT JOIN，且 ON/USING 必须覆盖目标表完整唯一键；
    汇总若引用会重复的关联表数值则拒绝。无法认证时抛出 QuerySemanticsError。
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
    """收集 USING/ON 等值条件，得到（右侧字段, 左侧字段）配对。"""
    pairs = []
    # 注意：QuerySemanticsError 是 ValueError 的子类，内部 _fail 抛出的
    # 错误会被下面的 except 捕获并再次追加提示，此行为需原样保留。
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
    """判断新加入的关联表在事实行中是否仍然唯一。"""
    # 一个维表行可在多条事实中重复出现；只有等值条件同时覆盖了
    # 此前已唯一的左侧键时，它自身才是唯一的。
    for left_alias in left_aliases & proof.unique_aliases:
        left_keys = {key.lower() for key in proof.aliases[left_alias].primary_key}
        if left_keys and left_keys <= {left[1] for right, left in pairs if left[0] == left_alias}:
            proof.unique_aliases.add(alias)


def _check_aggregates(query, proof):
    """拒绝引用了会重复的关联表数值的 SUM/AVG/COUNT 等汇总。"""
    for aggregate in query.find_all(exp.Func):
        if not isinstance(aggregate, exp.AggFunc) and not (
            isinstance(aggregate, exp.Anonymous) and aggregate.name.upper() == "TOTAL"
        ):
            continue
        # 嵌套 SELECT 内部的聚合不在本证明的认证范围内。
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
    """收集表达式中未被聚合函数或窗口函数包住的列节点。"""
    if isinstance(node, (exp.AggFunc, exp.Window)) or (
        isinstance(node, exp.Anonymous) and node.name.upper() == "TOTAL"
    ):
        return []
    if isinstance(node, exp.Column):
        return [node]
    return [column for child in node.iter_expressions() for column in free_column_nodes(child)]
