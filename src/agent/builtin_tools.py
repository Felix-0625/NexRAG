"""经过审计的本地小工具，不加载代码，也不访问文件系统或网络。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DISCOUNT_TOOL_ID = "builtin:discount_quote"
MAX_QUOTE_AMOUNT = Decimal("1000000000")


def calculate_discount_quote(amount, discount_percent) -> dict[str, str]:
    """用 Decimal 运算计算金额报价，分位按四舍五入（ROUND_HALF_UP）取整。"""
    try:
        if isinstance(amount, bool) or isinstance(discount_percent, bool):
            raise ValueError("Boolean values are not amounts")
        original, percent = Decimal(str(amount)), Decimal(str(discount_percent))
        if not original.is_finite() or not percent.is_finite():
            raise ValueError("Amount and discount must be finite")
        if not 0 <= original <= MAX_QUOTE_AMOUNT or not 0 <= percent <= 100:
            raise ValueError("Amount must be 0..1000000000 and discount 0..100")
        original = original.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        discount = (original * percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        quote = original - discount
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid amount or discount") from exc
    return {
        "original_amount": format(original, ".2f"),
        "discount_percent": str(percent),
        "discount_amount": format(discount, ".2f"),
        "quote_amount": format(quote, ".2f"),
    }
