"""针对公开 FinQA 运行中出现的错误百分比结论的回归用例。"""

import asyncio

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.agent.agent import AgenticRunner
from src.agent.dependencies import UserContext
from src.agent.evidence_arithmetic import EvidenceOperand, calculate
from src.agent.grounding import EvidenceLedger
from src.agent.prompts import DEFAULT_GUIDANCE
from src.agent.run_options import EvaluationRun


def test_money_difference_and_zero_cannot_acquire_percent_unit_from_numeric_membership():
    ledger = EvidenceLedger(required={"kb"})
    ledger.add("kb", [{"content": "undesignated hedges 42.1 and 41.2 million; source rate 5%."}])
    for a, b in (("42.1", "41.2"), ("42.1", "42.1")):
        ledger.add(
            "tool",
            calculate(
                ledger.entries,
                "subtract",
                EvidenceOperand(evidence_id="KB1", value=a, excerpt=f"hedges {a}"),
                EvidenceOperand(evidence_id="KB1", value=b, excerpt=b),
                1,
            ),
        )
    for answer in ("增长率0.9%。", "FINAL_ANSWER: 0.0%", r"Final: 0.9\%"):
        assert any("Unsupported percentage" in issue for issue in ledger.issues(answer))
    assert not ledger.issues("差值0.9 million; the source rate is 5%. [KB1]")
    result = calculate(
        ledger.entries,
        "percent_change",
        EvidenceOperand(evidence_id="KB1", value="42.1", excerpt="hedges 42.1"),
        EvidenceOperand(evidence_id="KB1", value="41.2", excerpt="41.2"),
        1,
    )
    ledger.add("tool", result)
    assert not ledger.issues(f"相对变化率{result['rounded_result']}%。 [KB1]")


def test_actual_agent_retries_false_percent_and_preserves_source_units_and_explanation():
    step = 0

    def model(messages, info):
        nonlocal step
        step += 1
        if step == 1:
            return ModelResponse([ToolCallPart("search_knowledge_documents", {"query": "hedges"})])
        if step == 2:
            return ModelResponse(
                [
                    ToolCallPart(
                        "calculate_evidence_arithmetic",
                        {
                            "operation": "subtract",
                            "left_evidence_id": "KB1",
                            "left_value": "42.1",
                            "right_evidence_id": "KB1",
                            "right_value": "41.2",
                            "precision": 1,
                        },
                    )
                ]
            )
        if step == 3:
            return ModelResponse([TextPart("The growth rate is 0.9%. [KB1]")])
        assert "Unsupported percentage" in str(messages[-1])
        return ModelResponse([TextPart("差额为0.9 million = 42.1 - 41.2。来源report.md [KB1]。")])

    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))
    evaluation = EvaluationRun(DEFAULT_GUIDANCE, {}, ["search_knowledge_documents"])
    evaluation.knowledge_documents = [
        {"content": "undesignated hedges 42.1 and 41.2 million", "source": "report.md"}
    ]

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                "仅检索知识库，计算差值，不查询业务数据库，不联网，不导出。",
                UserContext(user_id="arithmetic-claim", email="claim@local.test", tenant_id=""),
                evaluation=evaluation,
            )

    answer = asyncio.run(run())
    assert evaluation.status == "ok" and step == 4
    assert "0.9 million" in answer and "0.9%" not in answer
