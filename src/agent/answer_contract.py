"""Validate requested numeric deliverables separately from tool-call success.

Supported summaries require explicit labels: a detail table alone does not answer a count
request. This is a bounded output contract, not an arbitrary-text semantic judge.
"""

from __future__ import annotations

import re
from decimal import Decimal


def summary_issues(
    answer: str,
    required: set[str],
    counts: set,
    totals: set,
    *,
    result_row_counts: set | None = None,
    rendered_metrics: set[str] | None = None,
) -> list[str]:
    """Require labeled, evidence-backed values for explicitly requested summaries."""
    from src.agent.claim_formatting import is_post_discount_total

    plain = answer.replace("**", "").replace("`", "")
    fields = (
        (
            "db_count",
            r"总订单数|订单总数|订单数|总笔数|笔数|记录计数|记录数|order_count|record_count|order count|record count",
            counts,
            "count (e.g. 订单数：N)",
        ),
        (
            "db_total",
            r"总金额|订单总额|金额合计|合计金额|总收入|总销售额|金额列合计|total_amount|total amount",
            totals,
            "amount (e.g. 总金额：N)",
        ),
    )
    problems = []
    for capability, labels, expected, label in fields:
        if capability not in required or not expected:
            continue
        # Accept text, JSON, and two-column Markdown summaries, not matching numbers elsewhere.
        # Punctuation after a label is not a numeric value.
        # Reject malformed grouping and invalid amount prefixes rather than accepting partial numbers.
        pattern = rf"({labels})\s*[\"'：:为是=|]*\s*(\d+(?:,\d{{3}})*(?:\.\d+)?)(?!\d|\.\d|,[\d,])\s*(万|亿)?"
        values = [
            (
                name.lower(),
                Decimal(raw.replace(",", "")) * {"": 1, "万": 10000, "亿": 100000000}[unit or ""],
            )
            for match in re.finditer(pattern, plain, re.I)
            if capability != "db_total" or not is_post_discount_total(plain, match.start())
            for name, raw, unit in [match.groups()]
        ]
        # A joined result can contain three detail rows for two orders;
        # the requested order count must coexist with, not be replaced by, the result row count.
        row_labels = {"记录计数", "记录数", "record_count", "record count"}
        if capability not in (rendered_metrics or set()) and not any(
            value in expected for _, value in values
        ):
            problems.append(
                f"Missing requested answer field: {label}; state the verified value explicitly"
            )
        if any(
            value not in expected
            and not (
                capability == "db_count"
                and name in row_labels
                and value in (result_row_counts or set())
            )
            for name, value in values
        ):
            problems.append(f"Requested answer field {label} does not match query evidence")
    return problems
