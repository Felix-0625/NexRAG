"""将折扣运算绑定到本轮的数据库与制度证据。

纯计算器保持可复用；本层在 Agent 结果能满足派生金额任务之前，拒绝编造的输入金额、
冒充合计的 SQL 常量，以及有歧义的制度百分比。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import sqlglot
from sqlglot import exp

from src.agent.builtin_tools import DISCOUNT_TOOL_ID, calculate_discount_quote

CALCULATION = "discount_quote"
CENT = Decimal("0.01")
_PERCENT = re.compile(r"(?<![\w.])([0-9]+(?:\.[0-9]+)?)\s*[%％]")


def requested_discount(prompt: str) -> bool:
    """判断请求是否要求计算优惠或折后金额；只询问制度比例或上限属于检索，返回 False。"""
    from src.agent.task_intent import positive_request_text

    text = positive_request_text(prompt).lower()
    topic = re.search(r"优惠|折后|折扣金额|报价|discount|discounted|quote", text)
    explicit_math = re.search(
        r"计算|算出|算一下|算下|核算|测算|给出.*(?:优惠金额|折后金额)|calculate|compute|apply|quote",
        text,
    )
    money_question = re.search(
        r"(?:优惠|折后|折扣金额).{0,20}(?:多少钱|金额.*多少)|how much.{0,30}(?:discount|quote)",
        text,
    )
    # 只询问制度的比例/上限属于检索，不是金额计算任务。
    return bool(topic and (explicit_math or money_question))


def _decimal(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("布尔值不是业务金额或比例")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("业务金额或比例不是有效数字") from exc
    if not number.is_finite():
        raise ValueError("业务金额或比例必须是有限数字")
    return number


@dataclass(frozen=True)
class AmountBasis:
    evidence_id: str
    column: str
    amount: Decimal
    aggregate: bool


def _money_column(name):
    value = name.lower()
    return bool(
        re.search(r"amount|revenue|price|total|金额|收入|销售额", value)
        and not re.search(r"discount|优惠|折扣|quote|折后|count|数量|笔数", value)
    )


def amount_bases(entries) -> list[AmountBasis]:
    """接受简单的源列投影或 SUM(column)，不接受字面量。

    达到常规 50 行上限的明细结果可能被截断，不能确定完整金额，此时必须有真实的 SUM 查询。
    更复杂的 SQL 仍可用于回答，但必须暴露简单且已验证的金额投影，才能作为折扣的输入基数。
    """
    candidates = []
    for entry in entries:
        rows = entry.get("data")
        if entry["kind"] != "db" or not isinstance(rows, list) or not rows:
            continue
        try:
            query = sqlglot.parse_one(entry.get("sql", ""), read="sqlite")
        except sqlglot.errors.ParseError:
            continue
        if not isinstance(query, exp.Select) or not list(query.find_all(exp.Table)):
            continue
        if query.find(exp.CTE) or query.find(exp.Subquery):
            continue
        aggregate_query = any(isinstance(node, exp.AggFunc) for node in query.walk())
        for projection in query.expressions:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            aggregate = isinstance(expression, exp.Sum) and isinstance(expression.this, exp.Column)
            source = expression.this if aggregate else expression
            if not isinstance(source, exp.Column) or not _money_column(source.name):
                continue
            if aggregate and len(rows) != 1:
                continue
            if not aggregate and (
                aggregate_query
                or len(rows) >= 50
                or query.args.get("limit")
                or query.args.get("offset")
            ):
                continue
            key = projection.alias or projection.output_name or expression.sql(dialect="sqlite")
            # SQLite 的表达式列头可能仅在空白/大小写上有差异。
            actual_key = next(
                (
                    name
                    for name in rows[0]
                    if re.sub(r"\s", "", name).lower() == re.sub(r"\s", "", key).lower()
                ),
                None,
            )
            if actual_key is None:
                continue
            try:
                value = sum((_decimal(row[actual_key]) for row in rows), Decimal(0))
                money = value.quantize(CENT, rounding=ROUND_HALF_UP)
            except (KeyError, ValueError, InvalidOperation):
                continue
            candidates.append(AmountBasis(entry["id"], actual_key, money, aggregate))
    return candidates


def rate_bases(prompt, entries):
    """优先使用用户明确给出的百分比；否则使用带折扣标签的知识库事实。"""
    explicit = {_decimal(raw) for raw in _PERCENT.findall(prompt)}
    if explicit:
        return [("prompt", value) for value in sorted(explicit)]
    found = []
    for entry in entries:
        if entry["kind"] != "kb":
            continue
        for document in entry["data"] or []:
            for raw in re.findall(
                r"(?:折扣|优惠|discount)[^\n。；;，,]{0,30}?([0-9]+(?:\.[0-9]+)?)\s*[%％]",
                str(document.get("content", "")),
                re.I,
            ):
                found.append((entry["id"], _decimal(raw)))
    return found


def _select_amount_basis(entries, basis_evidence_id=None, amount_column=None):
    candidates = [
        basis
        for basis in amount_bases(entries)
        if (basis_evidence_id is None or basis.evidence_id == basis_evidence_id)
        and (amount_column is None or basis.column == amount_column)
    ]
    if not candidates:
        raise ValueError(
            "没有可用数据库金额证据；请先查询真实金额明细或 SUM(amount)，不能用 SELECT 常量代替"
        )
    if len({basis.amount for basis in candidates}) != 1:
        choices = ", ".join(
            f"{basis.evidence_id}.{basis.column}={basis.amount}" for basis in candidates
        )
        raise ValueError(
            "数据库存在多个金额基数，请指定 basis_evidence_id 和必要的 amount_column：" + choices
        )
    return sorted(candidates, key=lambda item: item.aggregate)[-1]


def _select_rate_basis(entries, prompt, rate_evidence_id=None):
    rates = [
        (identifier, value)
        for identifier, value in rate_bases(prompt, entries)
        if rate_evidence_id is None or identifier == rate_evidence_id
    ]
    if not rates or len({value for _, value in rates}) != 1:
        raise ValueError("折扣比例缺失或存在歧义，请明确比例或检索唯一政策并指定 rate_evidence_id")
    return rates[0]


def bind_discount_quote(
    entries,
    prompt,
    amount,
    discount_percent,
    *,
    basis_evidence_id=None,
    rate_evidence_id=None,
    amount_column=None,
):
    """为可复用的纯计算器 API 校验显式给出的数值输入。"""
    result = calculate_discount_quote(amount, discount_percent)
    basis = _select_amount_basis(entries, basis_evidence_id, amount_column)
    if _decimal(result["original_amount"]) != basis.amount:
        raise ValueError("计算基数与所选数据库总额证据不符，未计算优惠")
    rate_id, rate = _select_rate_basis(entries, prompt, rate_evidence_id)
    if _decimal(discount_percent) != rate:
        raise ValueError("折扣比例与当前问题或知识库政策证据不符，未计算优惠")
    binding = dict(
        basis_evidence_id=basis.evidence_id,
        amount_column=basis.column,
        rate_evidence_id=rate_id,
        formula="amount * discount_percent / 100",
        rounding="ROUND_HALF_UP to 0.01",
    )
    return result, binding


def quote_from_evidence(
    entries, prompt, *, basis_evidence_id=None, rate_evidence_id=None, amount_column=None
):
    """由 Agent 选择证据，金额和百分比单位始终由服务器掌控。

    LLM 不会抄写任何金额或比例，因此制度中的 6% 不会变成 0.06 个百分点；证据冲突时仍需显式指定 ID。
    """
    basis = _select_amount_basis(entries, basis_evidence_id, amount_column)
    rate_id, rate = _select_rate_basis(entries, prompt, rate_evidence_id)
    return bind_discount_quote(
        entries,
        prompt,
        basis.amount,
        rate,
        basis_evidence_id=basis.evidence_id,
        rate_evidence_id=rate_id,
        amount_column=basis.column,
    )


def validated_quotes(entries):
    """筛出账本中由内置折扣工具生成且带有证据绑定的报价条目。"""
    return [
        entry
        for entry in entries
        if entry["kind"] == "tool"
        and entry.get("tool_id") == DISCOUNT_TOOL_ID
        and entry.get("calculation_type") == CALCULATION
        and entry.get("binding")
    ]


def _labelled_amounts(answer, label, *, source_total=False):
    values = set()
    # 只去掉千分位逗号；分句逗号仍要阻止某个字段借用下一个字段的无关金额。
    answer = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", answer)
    for match in re.finditer(label + r"([^\n。；;，,]{0,140})", answer, re.I):
        if source_total:
            from src.agent.claim_formatting import is_post_discount_total

            if is_post_discount_total(answer, match.start()):
                continue
        body = match.group(1)
        # 字段值必须紧跟在标签/标点之后。
        # 像 '总金额与 KB1 的6%比例' 这样的文字是证据引用，并非声称数据库合计等于 1 或 6。
        if not re.match(r"[：:为是\s￥¥$\"']*-?[0-9]", body):
            continue
        # 展示的公式在最后一个 '=' 之前可能含有基数/比例。
        tail = re.split(r"=|＝", body)[-1]
        found = re.search(r"(?:[：:为是\s￥¥$\"'])*(-?[0-9]+(?:\.[0-9]+)?)(万|亿)?", tail)
        if found:
            if re.match(r"\s*[%％]", tail[found.end() :]):
                continue
            scale = {None: 1, "万": 10_000, "亿": 100_000_000}[found.group(2)]
            values.add((_decimal(found.group(1)) * scale).quantize(CENT, rounding=ROUND_HALF_UP))
    return values


def quote_answer_issues(answer, entries, prompt):
    """对已验证的报价，不仅检查数值是否存在，也检查字段含义。"""
    quotes = validated_quotes(entries)
    if not quotes:
        return []
    discount = _labelled_amounts(
        answer, r"(?:优惠(?:金额|额|合计)?(?!上限|比例|率)|折扣(?:金额|额)|discount[_ ]amount)"
    )
    net = _labelled_amounts(
        answer, r"(?:折后(?:总金额|金额|报价|总额|额)?|quote[_ ]amount|discounted[_ ]amount)"
    )
    totals = _labelled_amounts(
        answer, r"(?:总金额|订单总额|金额合计|合计金额|总收入|总销售额)", source_total=True
    )
    expected_discount = {_decimal(entry["data"]["discount_amount"]) for entry in quotes}
    expected_net = {_decimal(entry["data"]["quote_amount"]) for entry in quotes}
    expected_base = {_decimal(entry["data"]["original_amount"]) for entry in quotes}
    problems = []
    if discount - expected_discount or net - expected_net:
        problems.append("Discount or quote amount does not match the verified calculation")
    if requested_discount(prompt) and not discount and not net:
        problems.append("Verified discount calculation must be included in the final answer")
    if requested_discount(prompt):
        if re.search(r"优惠|折扣金额|discount[_ ]amount", prompt, re.I) and not discount:
            problems.append("Requested discount amount is missing from the final answer")
        if re.search(r"折后|quote|discounted", prompt, re.I) and not net:
            problems.append("Requested post-discount quote is missing from the final answer")
    if totals and len(expected_base) == 1 and totals != expected_base:
        problems.append("Discount calculation basis does not match the stated database total")
    return problems


def discount_retry_hint(entries, prompt):
    """生成折扣工具重试提示：列出账本中可用的金额基数与比例证据，要求只传证据选择器。"""
    bases = (
        ", ".join(
            f"{basis.evidence_id}.{basis.column}={basis.amount}" for basis in amount_bases(entries)
        )
        or "query SUM(amount) over the authorized business view"
    )
    rates = (
        ", ".join(f"{identifier}={value}%" for identifier, value in rate_bases(prompt, entries))
        or "retrieve the actual discount policy"
    )
    return (
        "Use calculate_discount_quote with evidence selectors only; do not pass amount or discount_percent. "
        f"Available bases: {bases}. Rate evidence: {rates}. "
        "Specify basis_evidence_id/rate_evidence_id when ambiguous. Do not repeat successful SQL/KB calls or export a file unless requested."
    )


def discount_retry_context(entries, prompt):
    """在工具调用无效后，暴露 ledger 中真实的选择器。

    文件名、集合 ID 和任意引用都不会被接受为证据 ID。此回调只报告真实的选择器，
    不会悄悄修正不可信的选择，也不会放宽绑定校验。
    """
    return {
        "available_bases": [
            {
                "basis_evidence_id": basis.evidence_id,
                "amount_column": basis.column,
                "amount": format(basis.amount, ".2f"),
            }
            for basis in amount_bases(entries)
        ],
        "available_rates": [
            {"rate_evidence_id": identifier, "discount_percent": str(value)}
            for identifier, value in rate_bases(prompt, entries)
        ],
        "reflection_hint": discount_retry_hint(entries, prompt)
        + " Evidence IDs are the returned DB1/KB1-style ledger IDs (or prompt), not filenames or library IDs. "
        "When only one basis and one rate are available, omit optional evidence IDs and amount_column rather than guessing them.",
    }
