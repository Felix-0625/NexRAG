"""识别展示层语法，但不放过缺乏依据的业务数值。"""

import re
from decimal import Decimal


def prompt_reference_text(prompt: str) -> str:
    """展示精度不是用户提供的业务数量。"""
    text = re.sub(r"\b(?:to|with)\s+\d+\s+decimal\s+places?\b", "", prompt, flags=re.I)
    return re.sub(r"(?:保留|四舍五入(?:至|到)?)\s*\d+\s*位小数", "", text)


_SYMBOLIC_DISCOUNT = re.compile(
    r"\bamount\s*\*\s*(discount_percent|[0-9]+(?:\.[0-9]+)?)\s*/\s*"
    r"([0-9]+(?:\.[0-9]+)?)(?![\w.])"
    r"(?:\s*(?:=|＝)\s*([0-9][0-9,]*(?:\.[0-9]+)?))?"
)


def _verified_symbolic_formula(match, quotes):
    """将展示的简写费率/结果绑定到服务器实际的报价。"""
    rate, divisor, result = match.groups()
    return Decimal(divisor) == 100 and any(
        quote.get("binding", {}).get("formula") == "amount * discount_percent / 100"
        and (
            rate == "discount_percent"
            or Decimal(rate) == Decimal(quote["data"]["discount_percent"])
        )
        and (
            result is None
            or Decimal(result.replace(",", "")) == Decimal(quote["data"]["discount_amount"])
        )
        for quote in quotes
    )


def discount_formula_issues(text: str, quotes) -> list[str]:
    """校验展示的受支持公式语法，即使其中每个数字都是已知的。"""
    if not quotes:
        return []
    for match in _SYMBOLIC_DISCOUNT.finditer(text):
        if not _verified_symbolic_formula(match, quotes):
            return ["Displayed discount formula does not match the verified calculation"]
    for base, rate, divisor, discount in re.findall(
        r"(?<![\w.])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:×|\*|x)\s*"
        r"\(?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|÷)\s*([0-9]+(?:\.[0-9]+)?)\s*\)?"
        r"\s*(?:=|＝)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        text,
        re.I,
    ):
        actual = tuple(Decimal(value.replace(",", "")) for value in (base, rate, discount))
        if Decimal(divisor) != 100 or not any(
            actual
            == tuple(
                Decimal(quote["data"][key])
                for key in ("original_amount", "discount_percent", "discount_amount")
            )
            for quote in quotes
        ):
            return ["Displayed discount formula does not match the verified calculation"]
    return []


def is_post_discount_total(text: str, start: int) -> bool:
    """折后金额的标签不能被误认为源数据库的合计。"""
    prefix = text[max(0, start - 24) : start]
    return bool(re.search(r"(?:折后|优惠后|折扣后)[^\n。；;，,:：]{0,12}$", prefix))


def normalize_verified_formulas(text: str, quotes) -> str:
    """仅在完全经过验证的折扣等式中，把 100 视为除数。

    这刻意不是数值白名单：单独出现的 100 元费用、错误的费率或带有编造结果的公式都保持原样，
    交给常规数值检查拒绝。
    """
    if not quotes:
        return text
    # 经验证的简写（如 amount * 6 / 100）与工具的符号公式含义相同，只有其除数属于展示语法。
    text = _SYMBOLIC_DISCOUNT.sub(
        lambda match: (
            f"amount × {match[1]}%" if _verified_symbolic_formula(match, quotes) else match[0]
        ),
        text,
    )
    pattern = re.compile(
        r"(?<![\w.])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:×|\*|x)\s*"
        r"\(?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|÷)\s*100\s*\)?"
        r"\s*(?:=|＝)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        re.I,
    )

    def replace(match):
        base, rate, discount = (Decimal(value.replace(",", "")) for value in match.groups())
        for quote in quotes:
            data = quote["data"]
            if (
                base == Decimal(data["original_amount"])
                and rate == Decimal(data["discount_percent"])
                and discount == Decimal(data["discount_amount"])
            ):
                return f"{match[1]} × {match[2]}% = {match[3]}"
        return match[0]

    return pattern.sub(replace, text)


def normalize_arithmetic_presentation(text: str, entries, *, mask_formulas: bool = False) -> str:
    """仅在绑定来源的公式内部允许百分比缩放常数。

    单独出现的 100 元费用仍视为事实声明；Unicode 金额负号只在计算器确实得到负结果时才做归一化。
    """
    # 转换仅用于显示的 LaTeX 写法；每个数值标记仍受下面同样的来源和算术检查约束。
    text = re.sub(r"\\text\{([^{}]*)\}", r"\1", text)
    text = text.replace(r"\left", "").replace(r"\right", "").replace(r"\times", "×")
    text = re.sub(
        r"\\frac\{([^{}]+)\}\{([^{}]+)\}",
        lambda m: f"({m[1]}) / {m[2]}" if re.search(r"\s[-+]\s", m[1]) else f"{m[1]} / {m[2]}",
        text,
    )
    calculations = [
        e["data"]
        for e in entries
        if e["kind"] == "tool"
        and isinstance(e["data"], dict)
        and e["data"].get("tool") == "calculate_evidence_arithmetic"
    ]
    from src.agent.arithmetic_syntax import normalize_numeric_formulas

    text = normalize_numeric_formulas(text, calculations, mask=mask_formulas)
    if any(c["operation"] in {"percentage", "percent_change"} for c in calculations):
        # 仅含变量的公式不引入数值操作数；单独的费用或含新数值的公式仍会被检查。
        symbolic = re.compile(
            r"[^\W\d_](?:[^\W\d_]|[\d _()−-]){1,80}\s*(?:/|÷)\s*"
            r"[^\W\d_](?:[^\W\d_]|[\d _()]){1,80}\s*[\]\)]*\s*(?:×|\*|x)\s*100(?!\w|\.\d)"
        )
        text = symbolic.sub(
            # 标签中可能含年份（如 "payment due in 2018"）；保留所有标签数字供常规 grounding 检查，只豁免缩放常数。
            lambda m: re.sub(r"(?:×|\*|x)\s*100$", "scaling" if mask_formulas else "%", m[0]),
            text,
        )
    for calculation in calculations:
        value = Decimal(calculation["result"])
        if value < 0:
            # "decreased by $N" 是实际负结果的绝对值，不是缺乏依据的正数金额。
            # 成员检查时保留符号；绝不把 +N 当作独立事实加入白名单。
            decrease = re.compile(
                r"(?:\b(?:decrease|decline|reduction|drop)(?:d)?\s+(?:of|by)\s*|(?:减少|下降|降低)(?:了|为)?)"
                r"[$€£]?\s*(\d+(?:,\d{3})*(?:\.\d+)?)(?![\d.])",
                re.I,
            )
            text = decrease.sub(
                lambda match: (
                    "change -" + match[1].replace(",", "")
                    if Decimal(match[1].replace(",", "")) == abs(value)
                    else match[0]
                ),
                text,
            )
            negative = re.compile(
                r"(?:−\s*[$€£]?\s*|-\s*[$€£]\s*)(\d+(?:,\d{3})*(?:\.\d+)?)(?![\d.])"
            )
            text = negative.sub(
                lambda match: (
                    "-" + match[1].replace(",", "")
                    if Decimal(match[1].replace(",", "")) == abs(value)
                    else match[0]
                ),
                text,
            )
    return text
