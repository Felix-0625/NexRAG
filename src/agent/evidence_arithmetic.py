"""Perform local Decimal arithmetic over values quoted from this turn's evidence.

The model selects operands and allowlisted operations, not code, files, SQL, or unsupported
numeric inputs. This proves arithmetic and provenance, not semantics or unit compatibility.
"""

from __future__ import annotations

import json
import operator
import re
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import ModelRetry, RunContext

from src.agent.dependencies import AgentDeps
from src.agent.grounding import _numbers

Operation = Literal["add", "subtract", "multiply", "divide", "percentage", "percent_change"]


class EvidenceOperand(BaseModel):
    """A numeric operand bound to a specific evidence quote."""

    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=1, max_length=32)
    excerpt: str = Field(min_length=1, max_length=400)
    value: Decimal = Field(max_digits=32, decimal_places=12, allow_inf_nan=False)


def _source_texts(entries, evidence_id: str) -> list[str]:
    entry = next((e for e in entries if e["id"] == evidence_id), None)
    if entry is None or entry["kind"] not in {"kb", "db", "file", "tool", "metric"}:
        available = [
            e["id"] for e in entries if e["kind"] in {"kb", "db", "file", "tool", "metric"}
        ]
        raise ValueError(
            f"Use a current-turn read evidence ID from {available}, not a filename, prompt or export"
        )
    if entry["kind"] == "kb":
        return [str(d.get("content", "")) for d in entry["data"]]
    return [json.dumps(entry["data"], ensure_ascii=False, default=str)]


def select_operand(entries, evidence_id: str, value: Decimal, excerpt: str = "") -> EvidenceOperand:
    """Resolve a value actually present in evidence without summing repeated representations.

    A report may repeat a number in text and tables; consume it once. An explicit excerpt can
    select context, but neither selection mode certifies semantics or compatible units.
    """
    if not excerpt:
        matches = []
        for text in _source_texts(entries, evidence_id):
            for line in text.splitlines():
                for match in re.finditer(
                    r"(?<![A-Za-z0-9_])(-?\d+(?:,\d{3})*(?:\.\d+)?)(万|亿)?", line
                ):
                    if value in _numbers(match.group(0)):
                        # Numeric tokens must remain intact inside source quotations.
                        matches.append(line.strip() if len(line) <= 400 else match.group(0))
        if not matches:
            raise ValueError(
                "Value is absent from its cited read evidence. Derived operands need prior TOOL evidence. "
                "For percentage change use percent_change with raw new and old source values; "
                "otherwise calculate the intermediate first, then cite its TOOL ID."
            )
        excerpt = matches[0]
    return EvidenceOperand(evidence_id=evidence_id, value=value, excerpt=excerpt)


def _operand(entries, operand: EvidenceOperand) -> dict:
    texts = _source_texts(entries, operand.evidence_id)
    # Whitespace may vary, but an excerpt must fit one returned document, not concatenated hits.
    excerpt = " ".join(operand.excerpt.split())
    if not any(excerpt in " ".join(text.split()) for text in texts):
        raise ValueError("Operand excerpt is absent from its cited read evidence")
    if operand.value not in _numbers(excerpt):
        raise ValueError("Operand value does not match a number in its cited excerpt")
    return {"evidence_id": operand.evidence_id, "excerpt": excerpt, "value": str(operand.value)}


def calculate(
    entries, operation: Operation, left: EvidenceOperand, right: EvidenceOperand, precision: int = 2
) -> dict:
    """Verify both quotes and values before Decimal arithmetic; reject zero divisors and bad
    precision.

    Retain raw results, rounded display values, and intermediates for output checks without
    inferring units or business definitions.
    """
    if operation not in {"add", "subtract", "multiply", "divide", "percentage", "percent_change"}:
        raise ValueError("Unsupported arithmetic operation")
    if isinstance(precision, bool) or not 0 <= precision <= 8:
        raise ValueError("Rounding precision must be between zero and eight")
    operands = [_operand(entries, left), _operand(entries, right)]
    left_value, right_value = left.value, right.value
    if operation in {"divide", "percentage", "percent_change"} and right_value == 0:
        raise ValueError("Cannot divide by zero")
    with localcontext() as context:
        context.prec = 80
        intermediates = {}
        if operation in {"add", "subtract", "multiply", "divide"}:
            result = {
                "add": operator.add,
                "subtract": operator.sub,
                "multiply": operator.mul,
                "divide": operator.truediv,
            }[operation](left_value, right_value)
            symbol = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}[operation]
            formula = f"{left_value} {symbol} {right_value}"
        elif operation == "percentage":
            result = left_value / right_value * 100
            formula = f"({left_value} / {right_value}) * 100"
            intermediates["ratio"] = str(left_value / right_value)
        else:
            result = (left_value - right_value) / right_value * 100
            formula = f"(({left_value} - {right_value}) / {right_value}) * 100"
            intermediates.update(
                difference=str(left_value - right_value),
                ratio=str((left_value - right_value) / right_value),
            )
        rounded = result.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
        display_values = {
            f"dp_{places}": str(result.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
            for places in range(9)
        }
    return {
        "tool": "calculate_evidence_arithmetic",
        "operation": operation,
        "formula": formula,
        "operands": operands,
        "result": str(result),
        "rounded_result": str(rounded),
        "display_values": display_values,
        "intermediate_values": intermediates,
    }


def register_arithmetic_tool(agent) -> None:
    """Register ledger-bound arithmetic and return correctable input failures for model retries."""

    @agent.tool(strict=False)
    def calculate_evidence_arithmetic(
        ctx: RunContext[AgentDeps],
        operation: Operation,
        left_evidence_id: str,
        left_value: Decimal,
        right_evidence_id: str,
        right_value: Decimal,
        precision: Annotated[int, Field(ge=0, le=8)] = 2,
        left_excerpt: str = "",
        right_excerpt: str = "",
    ) -> dict:
        """Compute two values quoted from current-turn evidence; never do mental arithmetic.

        Read first. Supply left_evidence_id, left_value, right_evidence_id and
        right_value as separate arguments. subtract means left_value-right_value.
        Differences between percentage values use subtract (percentage points),
        not percent_change (relative growth from an old baseline).
        The server locates real source quotes. To select a specific context supply an exact
        excerpt from document content, not metadata. percentage=left/right*100;
        percent_change=(left-right)/right*100.
        Use precision for decimal rounding, e.g. 0 for whole percent, 1 for one decimal.
        Use the server's rounded_result/display_values when formatting; do not
        invent intermediate values. Formula constants are recorded by the server.
        The result is local and read-only. It cannot access files, SQL or the network.
        """
        try:
            left = select_operand(
                ctx.deps.ledger.entries, left_evidence_id, left_value, left_excerpt
            )
            right = select_operand(
                ctx.deps.ledger.entries, right_evidence_id, right_value, right_excerpt
            )
            binding = None
            if operation == "percent_change":
                from src.agent.source_facts import bind_period_operands

                binding = bind_period_operands(
                    ctx.deps.ledger.prompt,
                    left,
                    right,
                    lambda evidence_id: _source_texts(ctx.deps.ledger.entries, evidence_id),
                )
            result = calculate(ctx.deps.ledger.entries, operation, left, right, precision)
            if binding is not None:
                result["temporal_binding"] = binding
        except ValueError as error:
            raise ModelRetry(str(error)) from error
        evidence_id = ctx.deps.ledger.add("tool", result)
        ctx.deps.evidence_calls += 1
        # Keep full Decimal and rounding provenance in the ledger, not model context;
        # long repeating decimals invite transcription errors and consume answer budget.
        return {
            "evidence_id": evidence_id,
            "operation": operation,
            "formula": result["formula"],
            "operands": result["operands"],
            "rounded_result": result["rounded_result"],
            "precision": precision,
            **({"temporal_binding": binding} if binding is not None else {}),
        }
