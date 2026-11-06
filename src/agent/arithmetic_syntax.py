"""将展示的数值等式与受信任的工具公式比对，绝不执行。

SQLGlot 已用于只读的业务表达式；复用它的 AST 可处理多余括号，而不会放过任意数值字面量。
"""

from __future__ import annotations

import re
from decimal import Decimal

import sqlglot
from sqlglot import exp


def _shape(node):
    if isinstance(node, exp.Paren):
        return _shape(node.this)
    if isinstance(node, exp.Literal) and not node.is_string:
        return Decimal(node.this)
    if isinstance(node, exp.Neg):
        child = _shape(node.this)
        if isinstance(child, Decimal):
            return -child
        raise ValueError("Only literal signs are supported")
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        return type(node).__name__, _shape(node.this), _shape(node.expression)
    raise ValueError("Only numeric arithmetic expressions are supported")


def _parse(text):
    return _shape(sqlglot.parse_one(text, read="sqlite"))


def normalize_numeric_formulas(text: str, calculations, *, mask: bool) -> str:
    """把文本中与受信任计算公式完全等价的算式归一化（末尾 ×100 改写为 %，mask 为真时整体替换）；其余原样保留。"""
    expected = set()
    for calculation in calculations:
        if calculation["operation"] not in {"percentage", "percent_change"}:
            continue
        expected.add(_parse(calculation["formula"]))
        if calculation["operation"] == "percent_change":
            left, right = (Decimal(o["value"]) for o in calculation["operands"])
            # 该中间式由同一个受信任的运算计算得出。
            expected.add(_parse(f"({left - right} / {right}) * 100"))
    if not expected:
        return text
    candidate = re.compile(r"(?<![\w.])[\d(][\d.,() +*/×÷−-]{2,400}")

    def replace(match):
        source = match[0]
        normalized = source.translate(str.maketrans({"×": "*", "÷": "/", "−": "-"}))
        try:
            normalized = re.sub(
                r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", lambda value: value[0].replace(",", ""), normalized
            )
            verified = _parse(normalized.strip()) in expected
        except (sqlglot.errors.ParseError, ValueError, ArithmeticError, RecursionError):
            verified = False
        if not verified:
            return source
        if mask:
            return "verified formula"
        # 只有末尾的百分比缩放常数属于展示层。
        return re.sub(r"(?:×|\*)\s*100(?:\.0+)?\s*$", "%", source)

    return candidate.sub(replace, text)
