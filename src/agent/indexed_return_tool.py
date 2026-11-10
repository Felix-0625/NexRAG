"""Read-only normalized-return calculations with evidence-controlled presentation."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated

from pydantic import Field
from pydantic_ai import ModelRetry, RunContext

from src.agent.dependencies import AgentDeps
from src.agent.evidence_arithmetic import EvidenceOperand, calculate
from src.agent.indexed_returns import completed_comparisons, prepare_comparison


def calculate_comparison(entries, binding, precision: int) -> dict:
    """Reuse Decimal arithmetic while preserving provenance of the actual base.

    Compute the equivalent (left-right)/base*100 to avoid rounding repeating intermediate
    returns before subtraction; retain individual returns in the provenance.
    """
    facts = binding["facts"]
    operands = [
        EvidenceOperand(
            evidence_id=fact["evidence_id"],
            value=fact["value"],
            # Keep headers with rows for semantic binding, but numeric quotes must remain contiguous source text.
            excerpt=fact["quote"].splitlines()[-1],
        )
        for fact in facts
    ]
    baseline = EvidenceOperand(
        evidence_id=binding["evidence_id"],
        value=binding["baseline_value"],
        excerpt=binding["baseline_quote"],
    )
    returns = [calculate(entries, "percent_change", operand, baseline, 8) for operand in operands]
    difference = calculate(entries, "subtract", *operands, precision=8)
    intermediate_id = "INDEXED_DIFFERENCE"
    local_entries = [*entries, {"id": intermediate_id, "kind": "tool", "data": difference}]
    delta = EvidenceOperand(
        evidence_id=intermediate_id,
        value=Decimal(difference["result"]),
        excerpt=difference["result"],
    )
    result = calculate(local_entries, "percentage", delta, baseline, precision)
    left, right = [fact["value"] for fact in facts]
    base = binding["baseline_value"]
    result.update(
        tool="calculate_indexed_return_difference",
        operation="indexed_return_difference",
        formula=f"(({left} - {base}) / {base} * 100) - (({right} - {base}) / {base} * 100)",
        unit="percentage points",
        comparison_binding=binding,
        component_returns=returns,
    )
    return result


def register_indexed_return_tool(agent) -> None:
    """Register the return-difference tool bound to this turn's sources, series order, and years."""

    @agent.tool(strict=False)
    def calculate_indexed_return_difference(
        ctx: RunContext[AgentDeps],
        evidence_id: str,
        left_label: str,
        right_label: str,
        period: str,
        precision: Annotated[int, Field(ge=0, le=8)] = 2,
    ) -> dict:
        """Compare normalized investment returns in percentage points.

        Read first. Use the server-prepared source labels, evidence ID and
        ending year. The source must state one shared initial investment;
        there is no default baseline. Raw ending-value subtraction is dollars,
        not a percentage-point comparison. No files, SQL or network are opened.
        """
        try:
            binding = prepare_comparison(ctx.deps.ledger.prompt, ctx.deps.ledger.entries)
            if binding is None:
                raise ValueError("This request does not bind an indexed-return comparison")
            supplied = (evidence_id, left_label, right_label, period)
            expected = tuple(
                binding[key] for key in ("evidence_id", "left_label", "right_label", "period")
            )
            if supplied != expected:
                raise ValueError(
                    "Use the current source-bound series order, evidence ID and ending year"
                )
            result = calculate_comparison(ctx.deps.ledger.entries, binding, precision)
        except ValueError as error:
            raise ModelRetry(str(error)) from error
        result_id = ctx.deps.ledger.add("tool", result, tool_id="builtin:indexed_return_difference")
        ctx.deps.evidence_calls += 1
        return {
            "evidence_id": result_id,
            "operation": result["operation"],
            "rounded_result": result["rounded_result"],
            "unit": result["unit"],
            "formula": result["formula"],
            "comparison_binding": binding,
        }


def render_comparison(report, ledger) -> str:
    """Render completed source-matching calculations, rejecting unrelated business metrics."""
    available = {entry["id"]: entry for entry in completed_comparisons(ledger)}
    if report.metric_ids or report.detail_evidence_ids:
        raise ValueError("Indexed investment report cannot cite unrelated database metrics")
    if not report.other_evidence_ids or not set(report.other_evidence_ids) <= available.keys():
        raise ValueError("Select a current source-bound indexed-return calculation")
    entry = available[report.other_evidence_ids[-1]]
    result = entry["data"]
    binding = result["comparison_binding"]
    known = {item["id"] for item in ledger.entries if item["kind"] == "kb" and item["data"]}
    selected = set(report.knowledge_evidence_ids)
    if binding["evidence_id"] not in selected or not selected <= known:
        raise ValueError("Indexed return report must cite its actual source evidence")

    def literal(value):
        # Escape Markdown syntax, not HTML: the cell renderer handles HTML and preserves readable ampersands.
        """Escape a value for safe literal Markdown rendering."""
        return re.sub(r"([\\`*_{}\[\]()#+.!|><&~-])", r"\\\1", str(value).replace("\n", " "))

    rows = "\n".join(
        f"| {literal(fact['metric'])} | {fact['period']} | {fact['value']} | {literal(fact['unit'])} |"
        for fact in binding["facts"]
    )
    return (
        "**累计收益率差**\n\n"
        f"共同初始投资：{binding['baseline_value']} {literal(binding['baseline_unit'])} [{binding['evidence_id']}]。\n\n"
        "| 投资序列 | 期末年份 | 期末投资值 | 来源单位 |\n| --- | --- | --- | --- |\n"
        + rows
        + f"\n\n收益率差：{result['rounded_result']} percentage points。\n\n"
        + f"计算式：`{result['formula']}`。\n\n计算依据：[{entry['id']}]。\n\n"
        + f"FINAL_ANSWER: {result['rounded_result']} percentage points"
    )
