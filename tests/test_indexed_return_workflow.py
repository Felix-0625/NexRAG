"""Normalized returns require a base, not just terminal-value subtraction."""

import asyncio

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.agent.agent import AgenticRunner
from src.agent.arithmetic_contract import requested_arithmetic
from src.agent.dependencies import UserContext
from src.agent.evidence_arithmetic import EvidenceOperand, calculate
from src.agent.grounding import EvidenceLedger, requested_capabilities
from src.agent.run_options import EvaluationRun

PROMPT = (
    "仅检索知识库，不查询业务数据库、不联网、不导出。"
    "What was the difference in percentage cumulative total shareholder return "
    "on alpha common stock versus the market index for the period ended 2024? "
    "Report the difference in percentage points."
)
SOURCE = (
    "The graph assumes investments of $250 on December 31, 2022 in our common stock "
    "and in each of the indices and the reinvestment of dividends.\n"
    "| | 2023 | 2024 |\n| alpha | $300 | $375 |\n| market index | $275 | $300 |"
)


def test_raw_subtraction_and_refusal_cannot_complete_the_normalized_return_request():
    ledger = EvidenceLedger(prompt=PROMPT, required=requested_capabilities(PROMPT, False))
    ledger.add("kb", [{"source": "public.md", "content": SOURCE}])
    left = EvidenceOperand(evidence_id="KB1", value="375", excerpt="| alpha | $300 | $375 |")
    right = EvidenceOperand(
        evidence_id="KB1", value="300", excerpt="| market index | $275 | $300 |"
    )
    ledger.add("tool", calculate(ledger.entries, "subtract", left, right))
    assert ledger.required == {"kb", "arithmetic:indexed_return_difference"}
    assert ledger.missing() == {"arithmetic:indexed_return_difference"}
    assert ledger.issues("The difference is $75; a percentage-point answer is not possible.")


def test_unresolved_explicit_comparison_keeps_the_contract_instead_of_throwing():
    assert requested_arithmetic("Return difference between two series in percentage points?") == {
        "arithmetic:indexed_return_difference"
    }


def test_actual_agent_retries_reversed_series_and_finishes_without_another_provider_call():
    calls = 0

    def model(messages, info):
        """Provide deterministic model responses for this scenario."""
        nonlocal calls
        calls += 1
        tools = {tool.name for tool in info.function_tools}
        if calls == 1:
            assert tools == {"search_knowledge_documents"}
            return ModelResponse(
                [ToolCallPart("search_knowledge_documents", {"query": "alpha returns"})]
            )
        assert tools == {"calculate_indexed_return_difference"}
        if calls == 3:
            assert "source-bound series order" in str(messages[-1])
        if calls > 3:
            raise AssertionError(
                "Certified report must finalize without another provider generation"
            )
        return ModelResponse(
            [
                ToolCallPart(
                    "calculate_indexed_return_difference",
                    {
                        "evidence_id": "KB1",
                        "left_label": "market index" if calls == 2 else "alpha",
                        "right_label": "alpha" if calls == 2 else "market index",
                        "period": "2024",
                        "precision": 2,
                    },
                )
            ]
        )

    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))
    evaluation = EvaluationRun("", {}, ["search_knowledge_documents"])
    evaluation.knowledge_documents = [{"source": "public.md", "content": SOURCE}]

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                PROMPT,
                UserContext(user_id="indexed-test", email="indexed@local.test", tenant_id=""),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert calls == 3 and evaluation.status == "ok"
    assert "250 USD" in answer
    assert "30.00 percentage points" in answer
    assert "public" in answer
    assert evaluation.messages[-1].model_name == "nexrag:verified-report-renderer"


def test_source_label_is_inert_markdown_without_double_escaped_ampersands():
    from src.agent.indexed_return_tool import calculate_comparison, render_comparison
    from src.agent.indexed_returns import prepare_comparison
    from src.agent.metric_results import report_from_evidence

    prompt = PROMPT.replace("market index", "a&p index")
    source = SOURCE.replace("market index", "a&p index")
    ledger = EvidenceLedger(prompt=prompt, required=requested_capabilities(prompt, False))
    ledger.add("kb", [{"source": "public.md", "content": source}])
    binding = prepare_comparison(prompt, ledger.entries)
    result = calculate_comparison(ledger.entries, binding, 2)
    ledger.add("tool", result, tool_id="builtin:indexed_return_difference")
    answer = render_comparison(report_from_evidence(ledger), ledger)
    assert r"a\&p index" in answer
    assert "&amp;" not in answer
