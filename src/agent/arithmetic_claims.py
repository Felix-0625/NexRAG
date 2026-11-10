"""Prevent computed scalars from acquiring unsupported percentage units.

This checks units and provenance, not financial semantics; answer-quality evaluation must
still verify the selected metrics, periods, and operands.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal


def _percentages(text: str) -> set[Decimal]:
    number = r"(-?\d+(?:,\d{3})*(?:\.\d+)?)"
    values = re.findall(r"(?<![A-Za-z0-9_])" + number + r"\s*(?:\\?[%％]|percent\b)", text, re.I)
    values += re.findall(r"百分之\s*" + number, text)
    return {Decimal(value.replace(",", "")) for value in values}


def arithmetic_percentage_issues(answer: str, entries) -> list[str]:
    """Require actual percentage arithmetic for derived percentages, including recorded rounding.

    Source percentages may be quoted unchanged. A known amount difference is not a percentage;
    this check neither adds a model call nor guesses business meaning.
    """
    calculations = [
        entry["data"]
        for entry in entries
        if entry["kind"] == "tool"
        and isinstance(entry["data"], dict)
        and entry["data"].get("tool") == "calculate_evidence_arithmetic"
    ]
    if not calculations:
        return []
    supported = set()
    for entry in entries:
        if entry["kind"] == "kb":
            texts = [str(document.get("content", "")) for document in entry["data"]]
        else:
            texts = [json.dumps(entry["data"], ensure_ascii=False, default=str)]
        for text in texts:
            supported |= _percentages(text)
        if entry["kind"] == "metric" and entry["data"].get("unit") in {"%", "percent", "百分比"}:
            if entry["data"].get("value") is not None:
                supported.add(Decimal(str(entry["data"]["value"])))
    from src.agent.derived_calculation import validated_quotes

    supported |= {Decimal(entry["data"]["discount_percent"]) for entry in validated_quotes(entries)}
    for calculation in calculations:
        if calculation["operation"] in {"percentage", "percent_change"}:
            supported.add(Decimal(calculation["result"]))
            supported.add(Decimal(calculation["rounded_result"]))
            supported |= {
                Decimal(value) for value in calculation.get("display_values", {}).values()
            }
    from src.agent.claim_formatting import normalize_arithmetic_presentation

    # Only source-bound equations may exempt the scaling constant in *100%.
    # Mask the verified equation so its normalized denominator cannot become a percentage claim.
    claims = normalize_arithmetic_presentation(answer, entries, mask_formulas=True)
    unsupported = _percentages(claims) - supported
    if unsupported:
        return [
            "Unsupported percentage claims: "
            + ", ".join(map(str, sorted(unsupported)))
            + ". A scalar difference or money amount cannot be relabeled as a percentage. "
            "For a derived share call percentage; for relative growth call percent_change "
            "with the correct raw source values. Unknown is not zero."
        ]
    return []
