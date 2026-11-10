"""Derived amounts require verified bases, rates, and exact calculations."""

import asyncio
from unittest.mock import Mock

import pytest

from src.agent.builtin_tools import DISCOUNT_TOOL_ID
from src.agent.dependencies import AgentDeps, UserContext
from src.agent.derived_calculation import (
    CALCULATION,
    bind_discount_quote,
    quote_answer_issues,
    quote_from_evidence,
    requested_discount,
)
from src.agent.grounding import EvidenceLedger, requested_capabilities
from src.agent.workspace_tools import build_workspace_toolset
from src.midplatform.data_policy import save_account_policy


def ledger():
    """Build an evidence ledger for the scenario."""
    evidence = EvidenceLedger(
        required={"db", "kb", CALCULATION}, prompt="查询订单金额，按制度计算优惠金额和折后金额"
    )
    evidence.add(
        "db", [{"total_amount": 2000}], sql="SELECT SUM(amount) AS total_amount FROM sales"
    )
    evidence.add("kb", [{"content": "企业折扣上限为6%。", "source": "policy"}])
    return evidence


def quote(evidence, amount=2000, rate=6, **kwargs):
    """Provide quote test data or behavior for this scenario."""
    result, binding = bind_discount_quote(evidence.entries, evidence.prompt, amount, rate, **kwargs)
    evidence.add(
        "tool", result, tool_id=DISCOUNT_TOOL_ID, calculation_type=CALCULATION, binding=binding
    )
    return result


def test_list_marker_is_not_a_business_number_but_item_money_is():
    evidence = ledger()
    quote(evidence)
    answer = "1. 总金额：2000元。\n2) 折扣上限6%。\n3. 优惠金额：120元。折后金额1880元。"
    assert evidence.issues(answer) == []
    assert evidence.issues(answer.replace("120元", "3元"))
    assert evidence.issues(answer.replace("120元", "121元"))


def test_reading_policy_does_not_require_discount_math():
    assert requested_capabilities("销售制度的折扣上限是多少？", True) == {"kb"}
    assert requested_discount("按制度计算订单合计可优惠多少钱")
    assert CALCULATION in requested_capabilities("按制度计算订单合计可优惠多少钱", True)
    assert not requested_discount("制度中的优惠比例是多少？")
    assert not requested_discount("优惠上限是多少？")
    assert requested_discount("本次订单合计能优惠多少钱？")
    assert requested_discount(
        "现在请调用上次用的折扣报价计算工具；如果权限已撤销就明确拒绝，不要自行计算。"
    )
    assert not requested_discount("请查询产品报价，不要计算优惠金额")


def test_revoked_calculator_is_denied_before_replaying_history_or_calling_model():
    from src.agent.agent import AgenticRunner
    from src.agent.run_options import EvaluationRun

    context = UserContext(user_id="denied-quote", email="denied-quote@local", tenant_id="")
    save_account_policy(context.email, {"allowed_tool_ids": []}, "test")
    runner = AgenticRunner()
    runner._agent = Mock()
    evaluation = EvaluationRun(guidance="", params={})
    answer = asyncio.run(
        runner.run(
            "现在请调用上次用的折扣报价计算工具；如果权限已撤销就明确拒绝，不要自行计算。",
            context,
            [("assistant", "此前报价1880；工具权限有效")],
            evaluation=evaluation,
        )
    )
    assert "未获授权" in answer and "未执行计算" in answer
    assert "1880" not in answer
    assert evaluation.status == "denied"
    runner._agent.run.assert_not_called()


def test_omitting_derived_task_is_incomplete_even_with_sql_and_kb():
    evidence = ledger()
    assert evidence.missing() == {CALCULATION}
    assert evidence.issues("总金额2000元，折扣上限6%。")
    assert "优惠计算" in evidence.safe_answer()
    assert "calculate_discount_quote" in evidence.retry_hint()
    assert "Do not repeat" in evidence.retry_hint()
    assert evidence.issues("优惠金额120元。")


def test_ready_quote_phase_excludes_repeated_reads_but_requires_actual_calculator():
    from types import SimpleNamespace

    from src.agent.metric_results import prepare_business_tools

    evidence = ledger()
    context = SimpleNamespace(deps=SimpleNamespace(ledger=evidence))
    definitions = [
        SimpleNamespace(name=name)
        for name in ["query_database", "search_knowledge_documents", "calculate_discount_quote"]
    ]
    prepared = asyncio.run(prepare_business_tools(context, definitions))
    assert [tool.name for tool in prepared] == ["calculate_discount_quote"]
    assert evidence.missing() == {CALCULATION}
    # Without permission, neither invent the tool nor fabricate success.
    assert asyncio.run(prepare_business_tools(context, definitions[:2])) == definitions[:2]
    evidence.add("kb", [{"content": "另一制度折扣上限8%。"}])
    assert asyncio.run(prepare_business_tools(context, definitions)) == definitions


def test_verified_quote_report_keeps_amount_roles_and_rejects_missing_tool_reference():
    from src.agent.metric_results import (
        BusinessReport,
        business_report_ready,
        render_business_body,
        report_from_evidence,
    )

    evidence = ledger()
    assert not business_report_ready(evidence)
    with pytest.raises(ValueError, match="缺少实际"):
        render_business_body(BusinessReport(), evidence)
    quote(evidence)
    assert business_report_ready(evidence)
    rendered = render_business_body(report_from_evidence(evidence), evidence)
    assert "总金额：2000.00" in rendered
    assert "优惠金额：120.00" in rendered
    assert "折后金额：1880.00" in rendered
    assert evidence.issues(rendered) == []
    assert evidence.issues(rendered.replace("折后金额：1880.00", "折后金额：9999.00"))
    omitted = report_from_evidence(evidence).model_copy(update={"other_evidence_ids": []})
    assert evidence.issues(render_business_body(omitted, evidence))


def test_quote_records_server_owned_provenance_and_semantics():
    evidence = ledger()
    assert quote(evidence) == {
        "original_amount": "2000.00",
        "discount_percent": "6",
        "discount_amount": "120.00",
        "quote_amount": "1880.00",
    }
    assert evidence.missing() == set()
    assert evidence.entries[-1]["binding"]["basis_evidence_id"] == "DB1"
    assert evidence.entries[-1]["binding"]["rate_evidence_id"] == "KB1"
    assert not evidence.issues("总金额2000元，优惠金额120元，折后金额1880元。")
    assert quote_answer_issues(
        "总金额2000元，优惠金额1880元，折后金额120元。", evidence.entries, evidence.prompt
    )
    assert quote_answer_issues("总金额2000元，折扣上限6%。", evidence.entries, evidence.prompt)


def test_currency_punctuation_and_rates_are_not_confused_with_discount_money():
    evidence = ledger()
    quote(evidence)
    assert not evidence.issues("总金额2,000元。优惠比例6%。优惠金额120元。折后金额1,880元。")
    assert not quote_answer_issues(
        "总金额0.2万元，优惠金额120元。折后金额1880元。", evidence.entries, evidence.prompt
    )
    assert quote_answer_issues("总金额2000元，优惠金额-120元。", evidence.entries, evidence.prompt)


def test_verified_percentage_formula_constants_are_local_to_the_formula():
    evidence = ledger()
    quote(evidence)
    assert not evidence.issues("总金额2000元。优惠金额：2000 × 6 / 100 = 120元。折后金额1880元。")
    assert evidence.issues("总金额2000元。优惠金额：2000 × 60 / 100 = 120元。折后金额1880元。")
    assert evidence.issues("总金额2000元。优惠金额120元。折后金额1880元。另有费用100元。")


def test_post_discount_total_is_not_the_database_original_total():
    evidence = ledger()
    quote(evidence)
    assert not evidence.issues("订单总额2000元。优惠金额120元。折后总金额1880元。")
    assert evidence.issues("订单总额1880元。优惠金额120元。折后总金额2000元。")


def test_actual_model_shorthand_and_prose_evidence_reference():
    evidence = ledger()
    quote(evidence)
    answer = (
        "总金额2000元。折扣金额120元。折后额1880元。\n"
        "计算依据：总金额与KB1中的6%比例，公式为 amount * 6 / 100。"
    )
    assert not evidence.issues(answer)
    for formula in ("amount * 6 / 120", "amount * 120 / 100", "amount * 6 / 100 = 1880"):
        assert evidence.issues(answer.replace("amount * 6 / 100", formula))
    assert evidence.issues(answer + "另有费用100元。")
    assert evidence.issues(answer.replace("折后额1880", "折后额120"))


def test_exact_tool_symbolic_formula_is_valid_but_not_a_standalone_fee():
    evidence = ledger()
    quote(evidence)
    answer = (
        "总金额2000元。优惠金额120元。折后总金额1880元。公式：`amount * discount_percent / 100`。"
    )
    assert not evidence.issues(answer)
    assert evidence.issues(answer + "另有费用100元。")
    assert evidence.issues(answer.replace("discount_percent / 100", "discount_percent / 120"))
    assert evidence.issues("总金额2000元。优惠金额：2000 × 6 / 120 = 120元。折后金额1880元。")


def test_unsolicited_fake_export_is_rejected_even_when_called_an_example():
    evidence = ledger()
    quote(evidence)
    answer = "总金额2000元。优惠金额120元。折后总金额1880元。"
    assert evidence.issues(answer + "[导出分析报告](https://example.com/export/demo.md)（仅示例）")
    evidence.add(
        "export", {"filename": "real.md", "download_url": "/api/nexrag-download/own/real.md"}
    )
    assert not evidence.issues(answer + "[下载报告](/api/nexrag-download/own/real.md)")
    assert evidence.issues(answer + "[下载报告](/api/nexrag-download/own/real)")
    assert "calculate_discount_quote with" not in evidence.retry_hint()


def test_both_requested_money_fields_must_be_delivered():
    evidence = ledger()
    quote(evidence)
    assert any(
        "quote is missing" in issue for issue in evidence.issues("总金额2000元，优惠金额120元。")
    )
    assert any(
        "discount amount is missing" in issue
        for issue in evidence.issues("总金额2000元，折后金额1880元。")
    )


@pytest.mark.parametrize("amount,rate", [(18000, 6), (2000, 60), (1080, 6)])
def test_wrong_amount_or_rate_cannot_reuse_valid_db_total(amount, rate):
    with pytest.raises(ValueError):
        quote(ledger(), amount, rate)


def test_sql_constant_and_limited_rows_cannot_create_fake_calculation_basis():
    evidence = ledger()
    for sql in [
        "SELECT 18000 AS amount FROM sales",
        "SELECT SUM(18000) AS amount FROM sales",
        "SELECT amount FROM sales LIMIT 1",
    ]:
        evidence.entries[0].update(data=[{"amount": 18000}], sql=sql)
        with pytest.raises(ValueError, match="金额证据"):
            quote(evidence, 18000)


def test_ambiguous_amount_and_rate_need_evidence_selection():
    evidence = ledger()
    evidence.add(
        "db", [{"total_amount": 3000}], sql="SELECT SUM(amount) AS total_amount FROM other_sales"
    )
    evidence.add("kb", [{"content": "旧制度折扣上限8%。"}])
    with pytest.raises(ValueError, match="多个金额"):
        quote(evidence)
    with pytest.raises(ValueError, match="歧义"):
        quote(evidence, basis_evidence_id="DB1")
    result = quote(evidence, basis_evidence_id="DB1", rate_evidence_id="KB1")
    assert result["discount_amount"] == "120.00"


def test_safe_fallback_deduplicates_display_not_audit():
    evidence = ledger()
    evidence.add(
        "db", [{"total_amount": 2000}], sql="SELECT SUM(amount) AS total_amount FROM sales"
    )
    assert len(evidence.entries) == 3
    assert evidence.safe_answer().count("已核验数据库查询") == 1


def test_authorized_tool_callback_rejects_bad_basis_and_rechecks_revocation():
    context = UserContext(user_id="u", email="u@local", tenant_id="")
    deps = AgentDeps(context=context, ledger=ledger())
    save_account_policy(context.email, {"allowed_tool_ids": [DISCOUNT_TOOL_ID]}, "test")
    callback = build_workspace_toolset(deps).tools["calculate_discount_quote"].function
    assert "error" in asyncio.run(callback(basis_evidence_id="unknown"))
    assert not any(entry["kind"] == "tool" for entry in deps.ledger.entries)
    assert asyncio.run(callback())["calculation_basis"]["basis_evidence_id"] == "DB1"
    save_account_policy(context.email, {"allowed_tool_ids": []}, "test")
    assert "error" in asyncio.run(callback())


def test_bad_reference_returns_real_ledger_selectors_for_corrective_call():
    context = UserContext(user_id="u", email="u@local", tenant_id="")
    deps = AgentDeps(context=context, ledger=ledger())
    save_account_policy(context.email, {"allowed_tool_ids": [DISCOUNT_TOOL_ID]}, "test")
    callback = build_workspace_toolset(deps).tools["calculate_discount_quote"].function
    rejected = asyncio.run(callback(basis_evidence_id="sales", rate_evidence_id="policy.md"))
    assert "error" in rejected
    assert rejected["available_bases"] == [
        {"basis_evidence_id": "DB1", "amount_column": "total_amount", "amount": "2000.00"}
    ]
    assert rejected["available_rates"] == [{"rate_evidence_id": "KB1", "discount_percent": "6"}]
    assert "not filenames" in rejected["reflection_hint"]
    accepted = asyncio.run(callback(basis_evidence_id="DB1", rate_evidence_id="KB1"))
    assert accepted["discount_amount"] == "120.00"
    assert accepted["calculation_basis"]["rate_evidence_id"] == "KB1"


def test_agent_calculator_owns_numeric_units_and_rejects_ambiguous_evidence():
    evidence = ledger()
    result, binding = quote_from_evidence(evidence.entries, evidence.prompt)
    assert result["discount_percent"] == "6"
    assert result["discount_amount"] == "120.00"
    assert result["quote_amount"] == "1880.00"
    assert binding["rate_evidence_id"] == "KB1"
    with pytest.raises(TypeError):
        quote_from_evidence(evidence.entries, evidence.prompt, discount_percent=0.06)
    evidence.add("kb", [{"content": "另一政策折扣上限8%。"}])
    with pytest.raises(ValueError, match="歧义"):
        quote_from_evidence(evidence.entries, evidence.prompt)
    result, _ = quote_from_evidence(evidence.entries, evidence.prompt, rate_evidence_id="KB1")
    assert result["discount_amount"] == "120.00"


def test_fallback_has_partial_trace_status_and_no_completed_task(monkeypatch):
    from src.agent.agent import AgenticRunner
    from src.agent.execution import EvaluationRun

    runner = AgenticRunner()
    runner._agent = Mock()

    async def model(*a, **kw):
        """Provide deterministic model responses for this scenario."""
        kw["deps"].ledger.add("db", [{"amount": 2000}], sql="SELECT amount FROM sales")
        raise TimeoutError()

    runner._agent.run = model
    evaluation = EvaluationRun("", {})
    answer = asyncio.run(
        runner.run(
            "查询订单总金额",
            UserContext(user_id="u", email="u@local", tenant_id=""),
            evaluation=evaluation,
        )
    )
    assert "2000" in answer
    assert evaluation.status == "partial"
