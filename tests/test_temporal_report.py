"""经认证的时间序列报告保留原始单位，并拒绝虚构的参照值。"""

import pytest

from src.agent.evidence_arithmetic import EvidenceOperand, calculate
from src.agent.grounding import EvidenceLedger
from src.agent.metric_results import (
    business_report_ready,
    render_business_body,
    report_from_evidence,
)
from src.agent.source_facts import bind_period_operands


def ledger_for(unit):
    prompt = "What is the percentage change in cash flow hedges in 2011 compared to 2010?"
    text = f"Cash flow hedges were $153.7 {unit} and $139.9 {unit} in fiscal 2011 and 2010, respectively."
    ledger = EvidenceLedger(prompt=prompt, required={"kb", "arithmetic:percent_change"})
    ledger.add("kb", [{"source": "report.md", "content": text}])
    left = EvidenceOperand(evidence_id="KB1", value="153.7", excerpt=text)
    right = EvidenceOperand(evidence_id="KB1", value="139.9", excerpt=text)
    result = calculate(ledger.entries, "percent_change", left, right, precision=1)
    result["temporal_binding"] = bind_period_operands(prompt, left, right, lambda _: [text])
    ledger.add("tool", result)
    return ledger


@pytest.mark.parametrize("unit,expected", [("million", "USD million"), ("", "USD")])
def test_report_uses_certified_units_and_actual_formula_instead_of_guessing_scale(unit, expected):
    ledger = ledger_for(unit)
    assert business_report_ready(ledger)
    output = render_business_body(report_from_evidence(ledger), ledger)
    assert f"| 2011 | 153.7 | {expected} | [KB1] |" in output
    assert f"| 2010 | 139.9 | {expected} | [KB1] |" in output
    assert "((153.7 - 139.9) / 139.9) * 100" in output
    assert "9.9%" in output and "[TOOL1]" in output
    assert not ledger.issues(output)


def test_unbound_raw_arithmetic_is_not_a_structured_temporal_report():
    ledger = ledger_for("million")
    del ledger.entries[-1]["data"]["temporal_binding"]
    assert not business_report_ready(ledger)
    with pytest.raises(ValueError):
        render_business_body(report_from_evidence(ledger), ledger)


def test_complete_report_with_raw_large_usd_operands_passes_the_real_grounding_validator():
    prompt = "Report SYY/2006/page_71.pdf: what was the percentage change in total rental expense under operating leases from July 2, 2005 to July 1, 2006? Give values, units, formula and citation."
    text = "total rental expense under operating leases was $ 100690000 , $ 92710000 , and $ 86842000 in fiscal 2006 , 2005 and 2004 , respectively ."
    ledger = EvidenceLedger(prompt=prompt, required={"kb", "arithmetic:percent_change"})
    ledger.add("kb", [{"source": "public-report.md", "content": text}])
    left = EvidenceOperand(evidence_id="KB1", value="100690000", excerpt=text)
    right = EvidenceOperand(evidence_id="KB1", value="92710000", excerpt=text)
    result = calculate(ledger.entries, "percent_change", left, right, precision=0)
    result["temporal_binding"] = bind_period_operands(prompt, left, right, lambda _: [text])
    ledger.add("tool", result)
    answer = render_business_body(report_from_evidence(ledger), ledger)
    assert not ledger.issues(answer)
    assert "9%" in answer
    assert "| 2006 | 100690000 | USD | [KB1] |" in answer
    assert "| 2005 | 92710000 | USD | [KB1] |" in answer
    assert "million" not in answer


@pytest.mark.parametrize(
    "field,value",
    [
        ("other_evidence_ids", ["TOOL999"]),
        ("knowledge_evidence_ids", []),
        ("knowledge_evidence_ids", ["KB999"]),
        ("metric_ids", ["invented"]),
    ],
)
def test_report_cannot_select_nonexistent_or_unbound_result_references(field, value):
    ledger = ledger_for("million")
    report = report_from_evidence(ledger)
    setattr(report, field, value)
    with pytest.raises(ValueError):
        render_business_body(report, ledger)


@pytest.mark.parametrize("revoked", [None, "knowledge", "model", "business"])
def test_renderer_shortcut_runs_live_authorization_and_normal_output_validator(
    monkeypatch, revoked
):
    import asyncio
    from types import SimpleNamespace

    from pydantic_ai.models.function import FunctionModel

    from src.agent import agent as agent_module
    from src.agent import model_hooks
    from src.agent.dependencies import AgentDeps, UserContext
    from src.agent.run_options import EvaluationRun
    from src.midplatform import agent_models

    def model(*_):
        raise AssertionError("Completed certified report must not invoke a provider")

    async def refresh(_):
        if revoked == "knowledge":
            raise ValueError("knowledge revoked")
        return SimpleNamespace(check_model=lambda _: None)

    def require_model(*_):
        if revoked == "model":
            raise ValueError("model revoked")

    def require_business(*_):
        if revoked == "business":
            raise ValueError("business revoked")

    monkeypatch.setattr(model_hooks, "refresh_policy", refresh)
    monkeypatch.setattr(agent_models, "require_model_access", require_model)
    monkeypatch.setattr(model_hooks, "require_business", require_business)
    validated = []
    original_issues = EvidenceLedger.issues

    def issues(self, answer):
        validated.append(answer)
        return original_issues(self, answer)

    monkeypatch.setattr(EvidenceLedger, "issues", issues)
    ledger = ledger_for("million")
    deps = AgentDeps(
        context=UserContext(
            user_id="renderer-test",
            email="renderer@local.test",
            tenant_id="synthetic",
            enforce_live_authorization=True,
        ),
        ledger=ledger,
        evaluation=EvaluationRun("", {}, [], business_schema="synthetic readonly schema"),
    )
    agent = agent_module.AgenticRunner()._build_agent(FunctionModel(model))
    if revoked:
        with pytest.raises(ValueError, match=revoked + " revoked"):
            asyncio.run(agent.run(ledger.prompt, deps=deps))
        assert deps.completion_mode is None and not validated
    else:
        result = asyncio.run(agent.run(ledger.prompt, deps=deps))
        assert deps.completion_mode == "verified_report_renderer"
        assert validated and validated[-1] == result.output
        assert "9.9%" in result.output and "USD million" in result.output


def test_agent_retrieves_and_calculates_then_finalizes_without_third_provider_call():
    import asyncio

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from src.agent.agent import AgenticRunner
    from src.agent.dependencies import UserContext
    from src.agent.run_options import EvaluationRun

    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse([ToolCallPart("search_knowledge_documents", {"query": "hedges"})])
        if calls == 2:
            return ModelResponse(
                [
                    ToolCallPart(
                        "calculate_evidence_arithmetic",
                        {
                            "operation": "percent_change",
                            "left_evidence_id": "KB1",
                            "left_value": "153.7",
                            "right_evidence_id": "KB1",
                            "right_value": "139.9",
                            "precision": 1,
                        },
                    )
                ]
            )
        raise AssertionError(
            "No third provider request may rewrite certified units or delay completion"
        )

    fixture = ledger_for("million")
    evaluation = EvaluationRun("", {}, ["search_knowledge_documents"])
    evaluation.knowledge_documents = fixture.entries[0]["data"]
    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "仅检索知识库，不查询业务数据库。" + fixture.prompt,
                UserContext(user_id="renderer-test", email="renderer@local.test", tenant_id=""),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert calls == 2 and evaluation.status == "ok"
    assert "9.9%" in answer and "USD million" in answer and "report" in answer
    final = evaluation.messages[-1]
    assert final.model_name == "nexrag:verified-report-renderer"
    assert final.metadata["completion_mode"] == "verified_report_renderer"
