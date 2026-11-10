"""Bind temporal operands using source layout and the requested comparison direction.

Accept only labeled cells, clauses, and aligned lists. Reject ambiguous operands rather
than repairing model choices; unsupported layouts require additional source evidence.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

YEAR = r"(?<!\d)(?:19|20)\d{2}(?!\d)"
NUMBER = r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?!\w|\.\d)"


class PeriodRequest(BaseModel):
    """Explicit old and new periods for a temporal comparison."""

    model_config = ConfigDict(frozen=True)
    old_period: str
    new_period: str
    metric: str


class SourceFact(BaseModel):
    """One source-bound metric value with its period and unit context."""

    model_config = ConfigDict(frozen=True)
    evidence_id: str
    metric: str
    period: str
    unit: str
    value: Decimal
    quote: str


def period_request(prompt: str) -> PeriodRequest | None:
    """Read the explicitly requested direction without guessing old and new roles from date order."""
    clean = re.sub(r"\S*/(?:19|20)\d{2}/\S+", "", prompt)
    if len(set(re.findall(YEAR, clean))) < 2:
        return None
    direction = re.search(
        r"(?:\bfrom\b|从)(.*?)\s*(?:\bto\b|到|至)(.*?)(?=[?？。\n]|$)", clean, re.I
    )
    if direction:
        old, new = [set(re.findall(YEAR, group)) for group in direction.groups()]
    else:
        direction = re.search(
            rf"({YEAR}).{{0,35}}?(?:compar(?:e|ed)\s+(?:to|with)|versus|相比|对比).{{0,20}}?({YEAR})",
            clean,
            re.I,
        )
        if not direction:
            raise ValueError(
                "Temporal comparison direction is unbound; specify the old and new reporting periods"
            )
        new, old = ({direction[1]}, {direction[2]})
    if len(old) != 1 or len(new) != 1 or old == new:
        raise ValueError("Temporal comparison has ambiguous reporting periods")
    metric = re.search(
        r"(?:percent(?:age)?\s+change|growth\s+rate)\s+(?:in|of)\s+(.+?)(?=\s+(?:from|in\s+(?:19|20)\d{2}|for\s+(?:19|20)\d{2})\b|[?？。]|$)",
        clean,
        re.I,
    )
    if metric is None:
        metric = re.search(
            r"([\u4e00-\u9fff]{2,30})(?=从)|([\u4e00-\u9fff]{2,30})(?:增长率|变化率|变动率)", clean
        )
    label = next((part for part in metric.groups() if part), "").strip() if metric else ""
    if not label:
        raise ValueError("Temporal metric is unbound; specify the metric together with its periods")
    return PeriodRequest(old_period=old.pop(), new_period=new.pop(), metric=label)


def _unit(text: str) -> str:
    units = []
    for label, pattern in (
        ("USD", r"\$|\b(?:usd|dollars?)\b"),
        ("CNY", r"人民币|元|\bcny\b"),
        ("EUR", r"€|\beur\b"),
        ("GBP", r"£|\bgbp\b"),
        ("percent", r"%|percent|百分比"),
        ("million", r"\bmillion\b"),
        ("billion", r"\bbillion\b"),
        ("thousand", r"\bthousand\b"),
        ("万", r"万"),
        ("亿", r"亿"),
    ):
        if re.search(pattern, text, re.I):
            units.append(label)
    # A shared quote or header may contain multiple currencies or scales; it does not prove equal units.
    if (
        len(set(units) & {"USD", "CNY", "EUR", "GBP"}) > 1
        or len(set(units) & {"million", "billion", "thousand", "万", "亿"}) > 1
        or ("percent" in units and len(units) > 1)
    ):
        return ""
    return " ".join(units)


def _metric_matches(requested: str, actual: str) -> bool:
    wanted = set(re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]+", requested.lower())) - {
        "the",
        "under",
        "and",
        "of",
        "in",
    }
    present = set(re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]+", actual.lower()))
    return bool(wanted) and wanted <= present


def _fact(evidence_id, metric, period, raw, quote, unit_context) -> SourceFact:
    return SourceFact(
        evidence_id=evidence_id,
        metric=metric.strip(),
        period=period,
        unit=_unit(unit_context),
        value=Decimal(raw.replace(",", "")),
        quote=quote.strip(),
    )


def _table_row_facts(evidence_id, header, cells, line) -> list[SourceFact]:
    """Read year-header table rows as one fact per year column."""
    facts = []
    for index, year in header[1]:
        raw = re.fullmatch(r"\s*\$?\s*(" + NUMBER + r")\s*", cells[index])
        if raw:
            quote, unit_context = header[2] + "\n" + line, header[2] + " " + line
            facts.append(_fact(evidence_id, cells[0], year, raw[1], quote, unit_context))
    return facts


def _respectively_facts(evidence_id, clause, values, year_tokens) -> list[SourceFact]:
    """Pair nonoverlapping, equal-sized amount and year blocks explicitly aligned by
    'respectively'.
    """
    if len({token[0] for token in year_tokens}) != len(year_tokens):
        return []
    if not (values[-1].end() < year_tokens[0].start() or year_tokens[-1].end() < values[0].start()):
        return []
    prefix = clause[: min(values[0].start(), year_tokens[0].start())]
    return [
        _fact(evidence_id, prefix, year[0], value[0], clause, clause)
        for value, year in zip(values, year_tokens, strict=True)
    ]


def _single_pair_fact(evidence_id, clause, year, value) -> SourceFact:
    """Read one year and one value from a clause, using surrounding text as the metric label."""
    if year.start() < value.start():
        metric = clause[: year.start()] + " " + clause[year.end() : value.start()]
    else:
        metric = clause[: value.start()]
    return _fact(evidence_id, metric, year[0], value[0], clause, clause)


def _trailing_qualified_facts(evidence_id, clause, values, year_tokens) -> list[SourceFact]:
    """Read only an explicitly qualified trailing list after unrelated amounts.

    In '... $x for A and $y and $z, respectively, of B', bind only the adjacent amounts
    qualified by 'of B'; calendar numbers and earlier amount groups are not cells.
    """
    aligned = re.search(r"\brespectively\s*,?\s+of\s+(.+)", clause, re.I)
    if aligned is None or not 2 <= len(year_tokens) <= 8:
        return []
    money = [
        token
        for token in values
        if token.end() < aligned.start() and re.search(r"[$€£]\s*$", clause[: token.start()])
    ]
    selected = money[-len(year_tokens) :]
    separators = [clause[a.end() : b.start()] for a, b in zip(selected, selected[1:])]
    if (
        len(selected) != len(year_tokens)
        or len({token[0] for token in year_tokens}) != len(year_tokens)
        or year_tokens[-1].end() >= selected[0].start()
        or not all(
            re.fullmatch(r"[\s,$€£]*(?:(?:million|billion|thousand|and)[\s,$€£]*)*", part, re.I)
            for part in separators
        )
    ):
        return []
    facts = []
    for value, year in zip(selected, year_tokens, strict=True):
        currency = re.search(r"([$€£])\s*$", clause[: value.start()])[1]
        scale = re.match(r"\s*(million|billion|thousand)\b", clause[value.end() :], re.I)
        unit_context = currency + (" " + scale[1] if scale else "")
        facts.append(_fact(evidence_id, aligned[1], year[0], value[0], clause, unit_context))
    return facts


def _clause_facts(evidence_id, clause) -> list[SourceFact]:
    """Read a clause using mutually exclusive supported layouts, otherwise return no facts."""
    tokens = list(re.finditer(NUMBER, clause))
    year_tokens = [token for token in tokens if re.fullmatch(YEAR, token[0])]
    values = [token for token in tokens if token not in year_tokens]
    # Accept ordered lists only when the source explicitly aligns equal-sized nonoverlapping year and amount blocks.
    ordered = re.search(r"\brespectively\b|分别", clause, re.I)
    if ordered and 2 <= len(values) == len(year_tokens) <= 8:
        return _respectively_facts(evidence_id, clause, values, year_tokens)
    if len(year_tokens) == len(values) == 1:
        return [_single_pair_fact(evidence_id, clause, year_tokens[0], values[0])]
    return _trailing_qualified_facts(evidence_id, clause, values, year_tokens)


def source_facts(evidence_id: str, text: str) -> list[SourceFact]:
    """Extract unambiguous year-value pairs while retaining source context."""
    facts = []
    header = None
    for line in text.splitlines():
        if line.count("|") >= 2:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            columns = [(i, cell) for i, cell in enumerate(cells) if re.fullmatch(YEAR, cell)]
            if columns:
                header = (cells, columns, line)
                continue
            if header and len(cells) == len(header[0]):
                facts.extend(_table_row_facts(evidence_id, header, cells, line))
                continue
        else:
            header = None
        for clause in re.split(r";|；|(?<=[.!?])\s+(?=[A-Z])", line):
            facts.extend(_clause_facts(evidence_id, clause))
    return facts


def bind_period_operands(prompt, left, right, source_texts) -> dict | None:
    """Verify metric, period, and unit roles; reject nonunique bindings before calculation."""
    request = period_request(prompt)
    if request is None:
        return None
    selected = []
    for operand, period in ((left, request.new_period), (right, request.old_period)):
        fact = _resolve_fact(
            request, period, {operand.evidence_id: source_texts(operand.evidence_id)}
        )
        if operand.value != fact.value:
            raise ValueError(
                f"Wrong temporal operand: period {period} for {request.metric!r} is {fact.value} {fact.unit}, not {operand.value}. percent_change requires left=new ({request.new_period}), right=old ({request.old_period}); do not reuse earlier calculations"
            )
        selected.append(fact)
    if selected[0].unit != selected[1].unit:
        raise ValueError(
            "Temporal operands have different source units; normalize units using verified evidence first"
        )
    return {
        "request": request.model_dump(),
        "facts": [fact.model_dump(mode="json") for fact in selected],
    }


def _resolve_fact(request, period, evidence) -> SourceFact:
    candidates = [
        fact
        for evidence_id, texts in evidence.items()
        for text in texts
        for fact in source_facts(evidence_id, text)
        if _metric_matches(request.metric, fact.metric) and fact.period == period
    ]
    if len({(fact.value, fact.unit) for fact in candidates}) != 1 or not candidates[0].unit:
        raise ValueError(
            f"Cannot bind {request.metric!r} for period {period} to one source value and unit; provide a labelled source row or exact aligned period/value context"
        )
    return candidates[0]


def period_calculation_inputs(prompt: str, entries) -> dict | None:
    """Prepare verified source roles before model selection instead of relying on failure hints.

    Share input parsing with the arithmetic tool; prepare phase state without executing or
    claiming task completion.
    """
    request = period_request(prompt)
    if request is None:
        return None
    evidence = {
        entry["id"]: [str(doc.get("content", "")) for doc in entry["data"]]
        for entry in entries
        if entry["kind"] == "kb"
    }
    new = _resolve_fact(request, request.new_period, evidence)
    old = _resolve_fact(request, request.old_period, evidence)
    if new.unit != old.unit:
        raise ValueError("Temporal operands have different source units")
    return {
        "metric": request.metric,
        "unit": new.unit,
        "new_period": new.period,
        "old_period": old.period,
        "left_evidence_id": new.evidence_id,
        "left_value": str(new.value),
        "right_evidence_id": old.evidence_id,
        "right_value": str(old.value),
    }
