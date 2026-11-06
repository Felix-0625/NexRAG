"""防止计算得到的标量被贴上缺乏依据的百分比单位。

这是单位/来源检查，不是财务语义分类器；指标、期间和操作数是否选对，仍需靠答案质量评测。
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
    """金额差值不会因为数值已知就成为百分比。

    来源中的百分比仍可原样引用；派生百分比必须来自真实的百分比运算，包括其记录的取整结果。
    这里刻意不增加第二次模型调用，也不猜测业务含义。
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

    # 只有绑定来源的等式才可豁免 *100% 中的缩放常数。
    # 整个已验证等式都被遮蔽，避免其分母在展示归一化后被误当成百分比声明。
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
