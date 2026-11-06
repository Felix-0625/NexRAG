"""将用户要求的数值交付物与工具调用成功与否分开校验。

受支持的业务任务契约使用显式的汇总标签：仅有明细表不算回答了计数请求，即便其行数可以数出来。
这是有边界的输出契约，不是对任意文本的语义裁判。
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
    """对显式要求的汇总项，要求带标签且有证据支撑的数值。"""
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
        # 支持文本、JSON 和两列 Markdown 汇总；其他位置恰好出现相同数字不算完成该字段。
        # 必须有真实数字：标签后的标点不是数值。
        # 拒绝畸形的千分位分组，不把单独的逗号转成数值，也不把非法金额的前缀当作所求指标。
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
        # 连接查询可能返回覆盖两个订单的三条明细行。正确的订单数必须能与真实结果行数并存，
        # 但不能仅把该行数当作所求业务指标。
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
