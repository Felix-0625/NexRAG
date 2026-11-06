"""源文件布局、所请求的期间与算术角色必须一致。"""

from decimal import Decimal

import pytest

from src.agent.evidence_arithmetic import EvidenceOperand, calculate
from src.agent.grounding import EvidenceLedger
from src.agent.source_facts import bind_period_operands, period_request

QUESTION = "What was the percentage change in rental expense from July 2, 2023 to July 1, 2024?"
SOURCE = "Total rental expense was $120, $100 and $80 in fiscal 2024, 2023 and 2022, respectively."


def operand(value, evidence_id="KB1"):
    return EvidenceOperand(evidence_id=evidence_id, value=Decimal(value), excerpt=SOURCE)


def bind(text=SOURCE, prompt=QUESTION, left="120", right="100"):
    return bind_period_operands(prompt, operand(left), operand(right), lambda _: [text])


def test_ordered_source_lists_bind_periods_and_reject_swapped_arithmetic_roles():
    certificate = bind()
    assert certificate["request"] == {
        "new_period": "2024",
        "old_period": "2023",
        "metric": "rental expense",
    }
    assert [fact["value"] for fact in certificate["facts"]] == ["120", "100"]
    assert all(fact["unit"] == "USD" for fact in certificate["facts"])
    with pytest.raises(ValueError, match="Wrong temporal operand"):
        bind(left="100", right="120")


@pytest.mark.parametrize(
    "text",
    [
        "rental expense 2024: $120; rental expense 2023: $100",
        "| rental expense | 2024 | 2023 |\n|---|---|---|\n| rental expense | $120 | $100 |",
        "rental expense in fiscal 2024 and 2023 was $120 and $100, respectively.",
    ],
)
def test_different_explicit_source_layouts_produce_the_same_bound_facts(text):
    assert bind(text)["facts"][0]["value"] == "120"


def test_compared_to_direction_is_not_sorted_or_assumed_to_be_chronological():
    prompt = "What is the percentage change in rental expense in 2023 compared to 2024?"
    request = period_request(prompt)
    assert request.new_period == "2023" and request.old_period == "2024"
    assert bind(prompt=prompt, left="100", right="120")["facts"][0]["period"] == "2023"


@pytest.mark.parametrize(
    "text",
    [
        SOURCE + " Rental expense 2024: $130.",
        "Total rental expense was $120 and $100 in fiscal 2024, 2023 and 2022, respectively.",
        "Operating income was $120 and $100 in fiscal 2024 and 2023, respectively.",
        "rental expense 2024: $120; rental expense 2023: €100",
        "rental expense 2024: 120; rental expense 2023: 100",
        "Total rental expense was $120 and €100 in fiscal 2024 and 2023, respectively.",
        "Total rental expense was $120 million and $100 billion in fiscal 2024 and 2023, respectively.",
        "| metric | 2024 | 2023 |\n| rental expense | $120 | €100 |",
    ],
)
def test_ambiguity_metric_mismatch_and_unbound_or_different_units_fail_closed(text):
    with pytest.raises(ValueError):
        bind(text)


def test_a_real_arithmetic_result_without_period_binding_cannot_complete_temporal_task():
    ledger = EvidenceLedger(prompt=QUESTION, required={"kb", "arithmetic:percent_change"})
    ledger.add("kb", [{"content": SOURCE}])
    result = calculate(ledger.entries, "percent_change", operand("120"), operand("100"))
    ledger.add("tool", result)
    assert "arithmetic:percent_change" in ledger.missing()
    result["temporal_binding"] = bind()
    assert not ledger.missing()


def test_original_report_grammar_uses_metric_periods_and_values_without_report_specific_rules():
    prompt = "Report SYY/2006/page_71.pdf: what was the percentage change in total rental expense under operating leases from july 2 , 2005 to july 1 , 2006? Give source values and citation."
    text = "total rental expense under operating leases was $ 100690000 , $ 92710000 , and $ 86842000 in fiscal 2006 , 2005 and 2004 , respectively ."
    certificate = bind(text, prompt, "100690000", "92710000")
    assert certificate["request"]["new_period"] == "2006"
    with pytest.raises(ValueError, match="Wrong temporal operand"):
        bind(text, prompt, "92710000", "100690000")


def test_qualified_money_sublist_does_not_bind_calendar_days_or_other_metric_amount():
    prompt = "What is the percentage change in cash flow hedges in 2011 compared to 2010?"
    text = "the total notional amounts of derivative instruments designated as hedging instruments as of october 29 , 2011 and october 30 , 2010 were $ 375 million of interest rate swap agreements accounted for as fair value hedges and $ 153.7 million and $ 139.9 million , respectively , of cash flow hedges denominated in euros ."
    certificate = bind(text, prompt, "153.7", "139.9")
    assert [fact["value"] for fact in certificate["facts"]] == ["153.7", "139.9"]
    assert certificate["facts"][0]["unit"] == "USD million"
    with pytest.raises(ValueError, match="Wrong temporal operand"):
        bind(text, prompt, "375", "139.9")
    # 没有明确的末尾指标限定词，或中间夹有其他度量时，
    # 直接取最后两个数字就是语义上的猜测。
    with pytest.raises(ValueError):
        bind(text.replace("respectively , of", "respectively , for"), prompt, "153.7", "139.9")
    with pytest.raises(ValueError):
        bind(
            text.replace("million and $ 139.9", "million for unrelated cost and $ 139.9"),
            prompt,
            "153.7",
            "139.9",
        )


def test_temporal_comparison_with_unrecognized_direction_does_not_receive_a_certificate():
    with pytest.raises(ValueError, match="direction"):
        bind(prompt="Percentage change in rental expense: 2023 and 2024?")
    assert period_request("Calculate growth from current and previous amounts") is None


def test_registered_tool_retries_swapped_roles_before_ledger_mutation_then_completes():
    import asyncio

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from src.agent.agent import AgenticRunner
    from src.agent.dependencies import UserContext
    from src.agent.prompts import DEFAULT_GUIDANCE
    from src.agent.run_options import EvaluationRun

    step = 0

    def model(messages, info):
        nonlocal step
        step += 1
        if step == 1:
            assert {tool.name for tool in info.function_tools} == {"search_knowledge_documents"}
            return ModelResponse(
                [ToolCallPart("search_knowledge_documents", {"query": "rental expense"})]
            )
        if step in {2, 3}:
            assert {tool.name for tool in info.function_tools} == {"calculate_evidence_arithmetic"}
            assert "Server-verified temporal source facts are ready" in str(messages)
            assert '"left_value": "120"' in str(messages)
            assert '"new_period": "2024"' in str(messages)
            if step == 3:
                assert "Wrong temporal operand" in str(messages[-1])
                assert "period 2024" in str(messages[-1])
            return ModelResponse(
                [
                    ToolCallPart(
                        "calculate_evidence_arithmetic",
                        {
                            "operation": "percent_change",
                            "left_evidence_id": "KB1",
                            "left_value": "100" if step == 2 else "120",
                            "right_evidence_id": "KB1",
                            "right_value": "120" if step == 2 else "100",
                            "precision": 0,
                        },
                    )
                ]
            )
        raise AssertionError("Certified calculation must finalize without another model request")

    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))
    evaluation = EvaluationRun(DEFAULT_GUIDANCE, {}, ["search_knowledge_documents"])
    evaluation.knowledge_documents = [{"source": "public-report.md", "content": SOURCE}]

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "仅检索知识库，不查询业务数据库。" + QUESTION,
                UserContext(user_id="period-test", email="period-test@local.test", tenant_id=""),
                evaluation=evaluation,
            )

    output = asyncio.run(run())
    assert evaluation.status == "ok" and step == 3
    assert "20%" in output and "public" in output
    assert "in millions" not in output
    assert "| 2023 | 100 | USD | [KB1] |" in output
    assert "| 2024 | 120 | USD | [KB1] |" in output
    calls = [
        part
        for message in evaluation.messages
        for part in message.parts
        if getattr(part, "part_kind", "") == "tool-return"
        and getattr(part, "tool_name", "") == "calculate_evidence_arithmetic"
    ]
    successes = [
        part
        for part in calls
        if isinstance(part.content, dict) and "temporal_binding" in part.content
    ]
    assert len(successes) == 1  # 被拒绝的反向调用永远不会成为成功的证据。
    assert successes[0].content["rounded_result"] == "20"


def test_ambiguous_temporal_facts_keep_collection_available_without_publishing_arguments():
    import asyncio
    from types import SimpleNamespace

    from src.agent.arithmetic_contract import document_instructions
    from src.agent.metric_results import prepare_business_tools

    ledger = EvidenceLedger(
        prompt="仅检索知识库，不查询业务数据库。" + QUESTION,
        required={"kb", "arithmetic:percent_change"},
    )
    ledger.add("kb", [{"content": SOURCE + " Rental expense 2024: $130."}])
    definitions = [
        SimpleNamespace(name=name)
        for name in (
            "search_knowledge_documents",
            "calculate_evidence_arithmetic",
            "query_database",
        )
    ]
    actual = asyncio.run(
        prepare_business_tools(SimpleNamespace(deps=SimpleNamespace(ledger=ledger)), definitions)
    )
    assert {tool.name for tool in actual} == {
        "search_knowledge_documents",
        "calculate_evidence_arithmetic",
    }
    assert "Server-verified temporal source facts are ready" not in document_instructions(ledger, 1)
