"""Document calculations require retrieval, calculation, and final reporting."""

import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.agent.agent import AgenticRunner
from src.agent.dependencies import UserContext
from src.agent.evidence_arithmetic import EvidenceOperand, calculate
from src.agent.grounding import EvidenceLedger, requested_capabilities
from src.agent.prompts import DEFAULT_GUIDANCE
from src.agent.run_options import EvaluationRun


def test_explicit_operation_contract_keeps_negative_and_definition_requests_separate():
    assert requested_capabilities("仅检索知识库，计算差值，不查询业务数据库。", False) == {
        "kb",
        "arithmetic:subtract",
    }
    assert requested_capabilities("检索文档，增长率是多少？", False) == {
        "kb",
        "arithmetic:percent_change",
    }
    assert requested_capabilities("检索知识库，不要计算增长率。", False) == {"kb"}
    assert requested_capabilities("知识库里的增长率公式是什么？", False) == {"kb"}


def ledger():
    """Build an evidence ledger for the scenario."""
    result = EvidenceLedger(required={"kb", "arithmetic:percent_change"})
    result.add("kb", [{"content": "current amount 17 million; previous amount 10 million."}])
    return result


def calculate_into(result, operation):
    """Provide calculate into test data or behavior for this scenario."""
    result.add(
        "tool",
        calculate(
            result.entries,
            operation,
            EvidenceOperand(evidence_id="KB1", value=17, excerpt="current amount 17 million"),
            EvidenceOperand(evidence_id="KB1", value=10, excerpt="previous amount 10 million"),
            precision=1,
        ),
    )


def test_retrieval_or_wrong_operation_cannot_complete_growth_question():
    result = ledger()
    assert result.missing() == {"arithmetic:percent_change"}
    calculate_into(result, "subtract")
    assert result.missing() == {"arithmetic:percent_change"}
    calculate_into(result, "percent_change")
    assert not result.missing()
    assert any(
        "Missing requested arithmetic result" in issue for issue in result.issues("原值17和10。")
    )
    assert not result.issues("增长率70.0%，由原值17和10计算。")
    assert not result.issues("增长率70.0%，保留0位小数。Rounded to 0 decimal places.")
    assert result.issues("增长率0%，保留0位小数。")


@pytest.mark.parametrize(
    "formula",
    [
        "(17 - 10) / 10 × 100%",
        r"\frac{17 - 10}{10} \times 100\%",
        r"\frac{(17 - 10)}{10} \times 100\%",
        "(((17 - 10)) / (10)) * 100%",
        "(本期 - 上期) / 上期 × 100%",
    ],
)
def test_verified_percent_scaling_is_presentation_but_standalone_fee_is_not(formula):
    result = ledger()
    calculate_into(result, "percent_change")
    assert not result.issues(f"增长率 = {formula} = 70.0%。")
    assert result.issues("增长率70.0%；额外费用100。")
    assert result.issues("增长率7 percent。")
    assert not result.issues("增长率百分之70.0。")


def test_symbolic_formula_may_name_period_but_cannot_hide_invented_values():
    result = ledger()
    result.entries[0]["data"][0]["content"] += " Report period 2018."
    calculate_into(result, "percent_change")
    assert not result.issues("增长率70.0%。Formula: (current 2018 amount / old amount) × 100.")
    assert result.issues("增长率70.0%。Formula: (current 99999 amount / old amount) × 100.")


def test_actual_agent_cannot_skip_compute_or_use_difference_as_growth():
    step = 0

    def model(messages, info):
        """Provide deterministic model responses for this scenario."""
        nonlocal step
        step += 1
        if step == 1:
            assert "Memory rules:" not in (messages[0].instructions or "")
            assert "db_join:" not in (messages[0].instructions or "")
            return ModelResponse([ToolCallPart("search_knowledge_documents", {"query": "growth"})])
        if step == 2:
            assert "calculate_evidence_arithmetic" in {tool.name for tool in info.function_tools}
            return ModelResponse([TextPart("文档未明确说明增长率，所以无法计算。")])
        if step == 3:
            assert "arithmetic:percent_change" in str(messages[-1])
            return ModelResponse(
                [
                    ToolCallPart(
                        "calculate_evidence_arithmetic",
                        {
                            "operation": "percent_change",
                            "left_evidence_id": "KB1",
                            "left_value": "17",
                            "right_evidence_id": "KB1",
                            "right_value": "10",
                            "precision": 1,
                        },
                    )
                ]
            )
        return ModelResponse(
            [
                TextPart(
                    "增长率70.0% = (17 - 10) / 10 × 100%，原值单位million。来源report.md [KB1]。"
                )
            ]
        )

    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))
    evaluation = EvaluationRun(DEFAULT_GUIDANCE, {}, ["search_knowledge_documents"])
    evaluation.knowledge_documents = [
        {"content": "current amount 17 million; previous amount 10 million.", "source": "report.md"}
    ]

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "仅检索知识库，增长率是多少？不查询业务数据库。",
                UserContext(user_id="contract", email="contract@local.test", tenant_id=""),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert evaluation.status == "ok" and step == 4
    assert "70.0%" in answer and "million" in answer
