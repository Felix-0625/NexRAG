"""对本轮读取证据中引用的数值做本地 Decimal 运算。

模型只能选择操作数和白名单内的运算，不能执行代码、打开文件、发起 SQL，
也不能凭空生成引用片段不支持的数值输入。这里证明的是算术与来源，而非业务含义或单位等价。
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
    """解析一个真实出现的数值；重复的表示形式不会累加。

    报告可能在正文和表格中重复同一个数字，运算只消费所选数值一次，而不是按出现次数计算。
    显式给出 excerpt 可以选定特定上下文；两种方式都不认证业务含义或单位兼容性。
    """
    if not excerpt:
        matches = []
        for text in _source_texts(entries, evidence_id):
            for line in text.splitlines():
                for match in re.finditer(
                    r"(?<![A-Za-z0-9_])(-?\d+(?:,\d{3})*(?:\.\d+)?)(万|亿)?", line
                ):
                    if value in _numbers(match.group(0)):
                        # 数值标记在来源引文中必须保持完整。
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
    # 空白差异不改变引文；但每段 excerpt 仍须完整出现在单个返回文档内，不能跨拼接的多条命中。
    excerpt = " ".join(operand.excerpt.split())
    if not any(excerpt in " ".join(text.split()) for text in texts):
        raise ValueError("Operand excerpt is absent from its cited read evidence")
    if operand.value not in _numbers(excerpt):
        raise ValueError("Operand value does not match a number in its cited excerpt")
    return {"evidence_id": operand.evidence_id, "excerpt": excerpt, "value": str(operand.value)}


def calculate(
    entries, operation: Operation, left: EvidenceOperand, right: EvidenceOperand, precision: int = 2
) -> dict:
    """先核实两端引文和数值，再做十进制定点运算；除零和非法精度直接拒绝。

    同时保留原始结果、舍入显示值与中间量供回答校验；不推断单位或业务口径。
    """
    if operation not in {"add", "subtract", "multiply", "divide", "percentage", "percent_change"}:
        raise ValueError("Unsupported arithmetic operation")
    if isinstance(precision, bool) or not 0 <= precision <= 8:
        raise ValueError("Rounding precision must be between zero and eight")
    operands = [_operand(entries, left), _operand(entries, right)]
    a, b = left.value, right.value
    if operation in {"divide", "percentage", "percent_change"} and b == 0:
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
            }[operation](a, b)
            symbol = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}[operation]
            formula = f"{a} {symbol} {b}"
        elif operation == "percentage":
            result = a / b * 100
            formula = f"({a} / {b}) * 100"
            intermediates["ratio"] = str(a / b)
        else:
            result = (a - b) / b * 100
            formula = f"(({a} - {b}) / {b}) * 100"
            intermediates.update(difference=str(a - b), ratio=str((a - b) / b))
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
    """注册只消费当前证据账本的运算工具，将可修正的输入错误交回模型重试。"""

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
        # 完整的 Decimal 精度/取整来源只存入 ledger，不放进模型上下文；
        # 冗长的循环小数容易引发抄写错误，还会白白占用最终答案的预算。
        return {
            "evidence_id": evidence_id,
            "operation": operation,
            "formula": result["formula"],
            "operands": result["operands"],
            "rounded_result": result["rounded_result"],
            "precision": precision,
            **({"temporal_binding": binding} if binding is not None else {}),
        }
