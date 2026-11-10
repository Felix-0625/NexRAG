"""Recognize presentation syntax without exempting unsupported business values."""

import re
from decimal import Decimal


def prompt_reference_text(prompt: str) -> str:
    """Exclude display precision from user-provided business quantities."""
    text = re.sub(r"\b(?:to|with)\s+\d+\s+decimal\s+places?\b", "", prompt, flags=re.I)
    return re.sub(r"(?:保留|四舍五入(?:至|到)?)\s*\d+\s*位小数", "", text)


_SYMBOLIC_DISCOUNT = re.compile(
    r"\bamount\s*\*\s*(discount_percent|[0-9]+(?:\.[0-9]+)?)\s*/\s*"
    r"([0-9]+(?:\.[0-9]+)?)(?![\w.])"
    r"(?:\s*(?:=|＝)\s*([0-9][0-9,]*(?:\.[0-9]+)?))?"
)


def _verified_symbolic_formula(match, quotes):
    """Bind displayed shorthand rates and results to the server's actual quote."""
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
    """Validate supported displayed formulas even when every number is already known."""
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
    """Distinguish a discounted-total label from the source database total."""
    prefix = text[max(0, start - 24) : start]
    return bool(re.search(r"(?:折后|优惠后|折扣后)[^\n。；;，,:：]{0,12}$", prefix))


def normalize_verified_formulas(text: str, quotes) -> str:
    """Treat 100 as a divisor only inside fully verified discount equations.

    This is not a numeric allowlist: standalone fees, wrong rates, and fabricated formula
    results
    remain unchanged for normal numeric validation to reject.
    """
    if not quotes:
        return text
    # Verified shorthand such as amount * 6 / 100 exempts only its presentation divisor.
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
        """Normalize only the matched verified expression."""
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
    """Allow percentage scaling constants only inside source-bound formulas.

    A standalone fee of 100 remains factual; normalize Unicode amount signs only when the
    calculator actually produced a negative result.
    """
    # Display-only LaTeX normalization does not bypass source or arithmetic checks.
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
        # Symbolic formulas add no numeric operands; standalone fees and new numbers remain checked.
        symbolic = re.compile(
            r"[^\W\d_](?:[^\W\d_]|[\d _()−-]){1,80}\s*(?:/|÷)\s*"
            r"[^\W\d_](?:[^\W\d_]|[\d _()]){1,80}\s*[\]\)]*\s*(?:×|\*|x)\s*100(?!\w|\.\d)"
        )
        text = symbolic.sub(
            # Keep years inside labels subject to grounding; exempt only the scaling constant.
            lambda m: re.sub(r"(?:×|\*|x)\s*100$", "scaling" if mask_formulas else "%", m[0]),
            text,
        )
    for calculation in calculations:
        value = Decimal(calculation["result"])
        if value < 0:
            # 'Decreased by $N' describes the absolute value of a real negative result.
            # Preserve the sign in membership checks; never allowlist +N as a separate fact.
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
