"""Source citations alone cannot excuse missing requested policy fields."""

from src.agent.grounding import EvidenceLedger
from src.agent.report_evidence import append_report_sources


def test_report_preserves_authorized_policy_text_without_model_rewrite():
    ledger = EvidenceLedger(required={"db", "kb"})
    text = "折扣上限6%。审批负责人陈经理。\n版本标识SYNTHETIC_V3。"
    ledger.add("kb", [{"source": "policy.md", "content": text}] * 2)
    output = append_report_sources("原总金额2000，优惠120，折后1880。", ledger)
    assert output.count("陈经理") == 1
    assert "> 版本标识SYNTHETIC_V3。" in output
    assert "[KB1] policy.md" in output
    assert "优惠120" in output


def test_missing_and_unrelated_knowledge_is_not_fabricated():
    assert append_report_sources("待核实", EvidenceLedger(required={"db", "kb"})) == "待核实"
    ledger = EvidenceLedger(required={"kb"})
    ledger.add("kb", [{"content": "仅知识问答不追加数据库报告面板"}])
    assert append_report_sources("正常知识答复", ledger) == "正常知识答复"


def test_document_answer_includes_only_actual_nonempty_source_identities():
    ledger = EvidenceLedger(required={"kb", "arithmetic:percent_change"})
    ledger.add("kb", [{"source": "report.md", "content": "Original report."}] * 2)
    ledger.add("kb", [{"source": "empty.md", "content": ""}])
    output = append_report_sources("Calculated from the report.", ledger)
    assert output.count("report\\.md") == 1
    assert "[KB1]" in output
    assert "empty.md" not in output
    assert "Original report." not in output


def test_source_name_cannot_become_a_link_or_html():
    ledger = EvidenceLedger(required={"kb"})
    ledger.add("kb", [{"source": "[click](https://outside.test)<img>", "content": "text"}])
    output = append_report_sources("Answer", ledger)
    assert r"\[click\]\(https://outside\\.test\)".replace("\\\\", "\\") in output
    assert "<img>" not in output
