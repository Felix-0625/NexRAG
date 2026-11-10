"""Bind discount calculations to this turn's database and policy evidence.

Keep the pure calculator reusable; reject invented amounts, SQL constants masquerading as
totals, and ambiguous policy percentages before accepting a derived-amount task.
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
    """Recognize discount calculations; rate-only and cap-only policy questions require retrieval."""
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
    # Rate-only and cap-only questions are retrieval tasks, not amount calculations.
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
    """Certified amount and its database evidence provenance."""

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
    """Accept simple source projections or SUM(column), never literal amounts.

    Details at the normal 50-row limit may be truncated and require a real SUM query. More
    complex SQL may answer questions but must expose a certified simple amount basis.
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
            # SQLite expression headers can differ only in whitespace or case.
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
    """Prefer explicit user percentages; otherwise use labeled knowledge-base discount facts."""
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
    """Validate explicit numeric inputs for the reusable pure calculator API."""
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
    """Let the agent select evidence while the server controls amounts and percentage units.

    The model never transcribes amounts or rates, so 6% cannot become 0.06 percentage points;
    conflicting evidence still requires an explicit ID.
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
    """Select ledger quotes from the built-in discount tool with verified evidence bindings."""
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
    # Remove grouping commas, but retain clause boundaries so fields cannot borrow unrelated amounts.
    answer = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", answer)
    for match in re.finditer(label + r"([^\n。；;，,]{0,140})", answer, re.I):
        if source_total:
            from src.agent.claim_formatting import is_post_discount_total

            if is_post_discount_total(answer, match.start()):
                continue
        body = match.group(1)
        # Values must immediately follow their labels or punctuation.
        # Evidence references and policy percentages do not assert a database total.
        if not re.match(r"[：:为是\s￥¥$\"']*-?[0-9]", body):
            continue
        # Displayed formulas can contain bases and rates before the final equals sign.
        tail = re.split(r"=|＝", body)[-1]
        found = re.search(r"(?:[：:为是\s￥¥$\"'])*(-?[0-9]+(?:\.[0-9]+)?)(万|亿)?", tail)
        if found:
            if re.match(r"\s*[%％]", tail[found.end() :]):
                continue
            scale = {None: 1, "万": 10_000, "亿": 100_000_000}[found.group(2)]
            values.add((_decimal(found.group(1)) * scale).quantize(CENT, rounding=ROUND_HALF_UP))
    return values


def quote_answer_issues(answer, entries, prompt):
    """Check quote field meaning as well as numeric presence."""
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
    """Describe available amount and rate selectors for a discount-tool retry."""
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
    """Expose only real ledger selectors after invalid tool input.

    Filenames, collection IDs, and arbitrary citations are not evidence IDs. Do not repair
    untrusted selections silently or relax binding validation.
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
