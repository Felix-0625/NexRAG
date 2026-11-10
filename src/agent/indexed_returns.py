"""Parse explicit normalized-investment comparisons from authorized text.

Require a stated common initial investment and labeled year columns. Do not infer bases,
entity synonyms, or missing units.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from src.agent.source_facts import YEAR, _unit, source_facts


class ComparisonRequest(BaseModel):
    """Explicit series, years, and direction for an investment comparison."""

    model_config = ConfigDict(frozen=True)
    left: str
    right: str
    period: str


def _label(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.lower())
    return " ".join(word for word in words if word not in {"the", "common", "stock", "index"})


def requested_comparison(prompt: str) -> ComparisonRequest | None:
    """Recognize only explicit return differences expressed in percentage points."""
    if not (
        re.search(r"return|收益", prompt, re.I)
        and re.search(r"percentage\s+points?|百分点", prompt, re.I)
        and re.search(r"difference|相差|差值|之差", prompt, re.I)
    ):
        return None
    clean = re.sub(r"\S*/(?:19|20)\d{2}/\S+", "", prompt)
    years = set(re.findall(YEAR, clean))
    ending = re.search(rf"(?:ended|ending|截至).{{0,35}}?({YEAR})", clean, re.I)
    if ending:
        period = ending[1]
    elif len(years) == 1:
        period = years.pop()
    else:
        raise ValueError("Specify one ending year for the indexed-return comparison")
    pair = re.search(
        r"(?:\bon\s+|\bbetween\s+)(.+?)\s+(?:versus|vs\.?|and)\s+(.+?)(?=\s+(?:for|over|as\s+of)\b|[?？。]|$)",
        clean,
        re.I,
    )
    if not pair or not _label(pair[1]) or not _label(pair[2]):
        raise ValueError("Specify the two source-labelled investment series to compare")
    if _label(pair[1]) == _label(pair[2]):
        raise ValueError("The investment comparison requires two distinct series")
    return ComparisonRequest(left=pair[1].strip(), right=pair[2].strip(), period=period)


def _baselines(text: str):
    for line in text.splitlines():
        # The assumption must apply to the stock and every index in the same table.
        # A negated assumption cannot prove the base; reject the clause rather than guess exceptions.
        if re.search(r"\bnot\b|\bnever\b|n't\b|并非|不假设|不采用", line, re.I):
            continue
        if not (
            re.search(r"common\s+stock", line, re.I)
            and re.search(r"each.{0,35}(?:indices|indexes|index)", line, re.I)
        ):
            continue
        amounts = re.findall(
            r"(?:assumes?\s+(?:the\s+)?investments?\s+of\s*|(?:of\s+)?a\s*)([$€£])\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)(?=\s+(?:investment\b|on\b))",
            line,
            re.I,
        )
        for currency, amount in amounts:
            value = Decimal(amount.replace(",", ""))
            unit = _unit(currency)
            periods = set(re.findall(YEAR, line))
            if unit and len(periods) == 1 and len(line.strip()) <= 400:
                yield value, unit, periods.pop(), line.strip()


def prepare_comparison(prompt: str, entries) -> dict | None:
    """Bind a common base and two terminal values from knowledge evidence; reject ambiguity or
    conflicts.
    """
    request = requested_comparison(prompt)
    if request is None:
        return None
    bindings = []
    for entry in entries:
        if entry["kind"] != "kb":
            continue
        for document in entry["data"]:
            text = str(document.get("content", ""))
            baselines = list(_baselines(text))
            if not baselines:
                continue
            if len({(value, unit, period) for value, unit, period, _ in baselines}) != 1:
                raise ValueError("Conflicting shared initial investments in the source")
            baseline, unit, baseline_period, baseline_quote = baselines[0]
            if baseline <= 0:
                raise ValueError("Shared initial investment must be positive")
            if int(baseline_period) >= int(request.period):
                raise ValueError("Initial investment must precede the requested ending year")
            facts = source_facts(entry["id"], text)
            selected = []
            for name in (request.left, request.right):
                matches = [
                    fact
                    for fact in facts
                    if fact.period == request.period and _label(fact.metric) == _label(name)
                ]
                if len({(fact.value, fact.unit) for fact in matches}) != 1:
                    break
                fact = matches[0]
                if fact.unit != unit:
                    raise ValueError(
                        "Initial investment and ending values need the same source unit"
                    )
                selected.append(fact)
            if len(selected) != 2:
                continue
            bindings.append(
                {
                    "request": request.model_dump(),
                    "evidence_id": entry["id"],
                    "left_label": selected[0].metric,
                    "right_label": selected[1].metric,
                    "period": request.period,
                    "baseline_value": str(baseline),
                    "baseline_unit": unit,
                    "baseline_period": baseline_period,
                    "baseline_quote": baseline_quote,
                    "facts": [fact.model_dump(mode="json") for fact in selected],
                }
            )
    identities = {
        (
            binding["baseline_value"],
            binding["baseline_unit"],
            *(fact["value"] for fact in binding["facts"]),
        )
        for binding in bindings
    }
    if len(identities) != 1:
        raise ValueError(
            "Cannot bind both series, ending year and shared initial investment unambiguously"
        )
    binding = bindings[0]
    binding["tool_arguments"] = {
        key: binding[key] for key in ("evidence_id", "left_label", "right_label", "period")
    }
    return binding


def verified_comparisons(entries, prompt: str) -> list[dict]:
    """Select request- and source-bound indexed-return differences measured in percentage points."""
    try:
        request = requested_comparison(prompt)
    except ValueError:
        return []
    if request is None:
        return []
    current_sources = {entry["id"] for entry in entries if entry["kind"] == "kb" and entry["data"]}
    return [
        entry
        for entry in entries
        if entry["kind"] == "tool"
        and entry.get("tool_id") == "builtin:indexed_return_difference"
        and isinstance(entry["data"], dict)
        and entry["data"].get("tool") == "calculate_indexed_return_difference"
        and entry["data"].get("operation") == "indexed_return_difference"
        and entry["data"].get("unit") == "percentage points"
        and entry["data"].get("comparison_binding", {}).get("evidence_id") in current_sources
        and entry["data"].get("comparison_binding", {}).get("request") == request.model_dump()
    ]


def completed_comparisons(ledger) -> list[dict]:
    """Return verified comparisons only for knowledge-plus-indexed-return tasks, otherwise no
    results.
    """
    if ledger.required != {"kb", "arithmetic:indexed_return_difference"}:
        return []
    return verified_comparisons(ledger.entries, ledger.prompt)
