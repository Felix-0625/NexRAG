"""模型输出的标点必须触发重试，而不能让数值校验崩溃。"""

from decimal import Decimal

from src.agent.answer_contract import summary_issues


def test_comma_after_label_is_not_a_number():
    assert summary_issues("订单数, 总金额：350。", {"db_count", "db_total"}, {2}, {350})
    assert summary_issues("总金额：,350", {"db_total"}, set(), {350})
    assert summary_issues("总金额：1,,350", {"db_total"}, set(), {1350})


def test_valid_grouped_amount_and_decimal_remain_supported():
    assert not summary_issues(
        "订单数：2，总金额：1,350.50。", {"db_count", "db_total"}, {2}, {Decimal("1350.50")}
    )
