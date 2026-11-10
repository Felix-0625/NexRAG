"""Explicit prohibitions must not silently create positive execution requirements."""

import pytest

from src.agent.grounding import requested_capabilities


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("调用public_sum把2和5相加。不要读取数据库或知识库，不要自己心算代替工具。", set()),
        ("不要修改数据库，只查询订单", {"db"}),
        ("查订单不要知识库", {"db"}),
        ("只查知识库不要读数据库", {"kb"}),
        ("只查KB不要读DB", {"kb"}),
        ("只查DB不要读KB", {"db"}),
        ("请用SQL查询", {"db"}),
        ("Explain feedback and kb2 identifiers", set()),
        ("不要查询订单总金额，而是检索企业制度", {"kb"}),
        ("请查询订单总金额；不要导出CSV。", {"db", "db_total"}),
        (
            "查询订单总金额，再按制度计算优惠金额。不要猜测。",
            {"db", "db_total", "kb", "discount_quote"},
        ),
        ("只读取知识库，不要计算优惠金额。", {"kb"}),
        ("查询发票明细和金额，不检索知识库，不读取其他公司。", {"db", "db_details"}),
        ("不查询数据库，只检索企业制度。", {"kb"}),
        ("查询订单金额与制度是否不一致。", {"db", "kb"}),
        ("仅查阅知识库，不查询业务数据库。", {"kb"}),
        ("只查询公开主题，不读取知识库、不查询业务数据库。", set()),
        ("只查询公开主题，不查询业务数据库；不要读取知识库。", set()),
        ("查找知识库里的负责人，将结果生成一个 Markdown 报告供我下载。", {"kb", "export"}),
        ("将结果生成一个文件供下载。", {"export"}),
        ("查询订单总金额，不要生成报告供我下载。", {"db", "db_total"}),
    ],
)
def test_negative_clauses_are_removed_only_from_required_task_detection(prompt, expected):
    assert requested_capabilities(prompt, True) == expected
