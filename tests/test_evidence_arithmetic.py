"""Derived numbers require real local calculations bound to current evidence."""

from decimal import Decimal

import pytest

from src.agent.evidence_arithmetic import EvidenceOperand, calculate, select_operand
from src.agent.grounding import EvidenceLedger


def operand(value, excerpt, evidence_id="KB1"):
    """Provide operand test data or behavior for this scenario."""
    return EvidenceOperand(evidence_id=evidence_id, excerpt=excerpt, value=value)


def test_finqa_reference_difference_becomes_supported_only_after_calculation():
    ledger = EvidenceLedger(required={"kb"})
    ledger.add("kb", [{"content": "2014 net revenue $5,735; 2015 net revenue $5,829."}])
    answer = "Net change: 94 million = 5829 - 5735. [KB1]"
    assert "Unsupported numeric claims: 94" in ledger.issues(answer)
    result = calculate(
        ledger.entries,
        "subtract",
        operand("5829", "2015 net revenue $5,829"),
        operand("5735", "2014 net revenue $5,735"),
    )
    assert Decimal(result["result"]) == 94
    ledger.add("tool", result)
    assert not ledger.issues(answer)


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("add", "15"),
        ("subtract", "5"),
        ("multiply", "50"),
        ("divide", "2"),
        ("percentage", "200"),
        ("percent_change", "100"),
    ],
)
def test_whitelisted_operations(operation, expected):
    entries = [{"id": "KB1", "kind": "kb", "data": [{"content": "a=10; b=5"}]}]
    result = calculate(entries, operation, operand(10, "a=10"), operand(5, "b=5"))
    assert Decimal(result["result"]) == Decimal(expected)


@pytest.mark.parametrize(
    "bad",
    [
        operand(94, "actual amount 5829"),
        operand(5829, "invented amount 5829"),
        operand(5829, "actual amount 5829", "KB99"),
        operand(1, '"score": 1'),
    ],
)
def test_fabricated_values_quotes_ids_or_metadata_are_rejected(bad):
    entries = [{"id": "KB1", "kind": "kb", "data": [{"content": "actual amount 5829", "score": 1}]}]
    with pytest.raises(ValueError):
        calculate(entries, "subtract", bad, operand(5829, "actual amount 5829"))


def test_exports_are_not_arithmetic_source_evidence():
    entries = [{"id": "EXPORT1", "kind": "export", "data": {"amount": 94}}]
    with pytest.raises(ValueError, match="read evidence ID"):
        calculate(entries, "add", operand(94, "94", "EXPORT1"), operand(94, "94", "EXPORT1"))


def test_server_resolves_unique_numeric_source_excerpt_without_model_copying_it():
    entries = [
        {"id": "KB1", "kind": "kb", "data": [{"content": "2015 net revenue $5,829 million"}]}
    ]
    selected = select_operand(entries, "KB1", Decimal(5829))
    assert "$5,829" in selected.excerpt
    assert selected.value == 5829


def test_source_excerpt_does_not_manufacture_truncated_numeric_fragments():
    from src.agent.grounding import _numbers

    text = "1" * 100 + " 2 " + "9" * 100
    entries = [{"id": "KB1", "kind": "kb", "data": [{"content": text}]}]
    selected = select_operand(entries, "KB1", Decimal(2))
    assert _numbers(selected.excerpt) <= _numbers(text)


def test_missing_numeric_occurrence_is_not_implicitly_selected():
    entries = [{"id": "KB1", "kind": "kb", "data": [{"content": "first 10; second 10"}]}]
    with pytest.raises(ValueError, match="absent"):
        select_operand(entries, "KB1", Decimal(94))


def test_repeated_prose_and_table_values_are_single_operands_not_extra_rows():
    entries = [
        {
            "id": "KB1",
            "kind": "kb",
            "data": [{"content": "leased total 8.1\n| leased | 8.1 |\n| total | 56.0 |"}],
        }
    ]
    left = select_operand(entries, "KB1", Decimal("8.1"))
    right = select_operand(entries, "KB1", Decimal("56.0"))
    result = calculate(entries, "percentage", left, right, precision=0)
    assert result["rounded_result"] == "14"


def test_zero_denominator_is_rejected():
    entries = [{"id": "KB1", "kind": "kb", "data": [{"content": "amount 10, denominator 0"}]}]
    with pytest.raises(ValueError, match="zero"):
        calculate(entries, "divide", operand(10, "amount 10"), operand(0, "denominator 0"))


def test_percent_change_uses_decimal_and_explicit_rounding():
    entries = [{"id": "KB1", "kind": "kb", "data": [{"content": "2011: 153.7; 2010: 139.9"}]}]
    result = calculate(
        entries,
        "percent_change",
        operand("153.7", "2011: 153.7"),
        operand("139.9", "2010: 139.9"),
        precision=1,
    )
    assert result["rounded_result"] == "9.9"


def test_actual_formula_and_rounded_display_values_support_financial_presentation():
    ledger = EvidenceLedger(required={"kb"})
    ledger.add("kb", [{"content": "stock awards 121.4; total purchase price 4187.8"}])
    assert ledger.issues("(121.4 / 4187.8) * 100 = 2.8989%, about 2.9%. [KB1]")
    result = calculate(
        ledger.entries,
        "percentage",
        operand("121.4", "stock awards 121.4"),
        operand("4187.8", "total purchase price 4187.8"),
        precision=1,
    )
    ledger.add("tool", result)
    assert not ledger.issues("(121.4 / 4187.8) * 100 = 2.8989%, about 2.9%. [KB1]")
    assert not ledger.issues(
        "Formula: (stock awards value / total purchase price) * 100. Result 2.9%. [KB1]"
    )
    assert ledger.issues("The result is 2.99%. [KB1]")
    assert ledger.issues("An extra fee of 100 dollars. [KB1]")
    assert not ledger.issues(r"\left(\frac{121.4}{4187.8}\right) \times 100 = 2.8989\%. [KB1]")
    assert ledger.issues(r"\left(\frac{121.4}{4187.8}\right) \times 100 = 2.99\%. [KB1]")


def test_percent_change_records_its_real_intermediate_difference():
    ledger = EvidenceLedger(required={"kb"})
    ledger.add("kb", [{"content": "new 153.7; old 139.9"}])
    result = calculate(
        ledger.entries,
        "percent_change",
        operand("153.7", "new 153.7"),
        operand("139.9", "old 139.9"),
        precision=1,
    )
    ledger.add("tool", result)
    assert result["intermediate_values"]["difference"] == "13.8"
    assert not ledger.issues(
        "(153.7 - 139.9) / 139.9 * 100 = 9.86%, rounded 9.9%. Difference 13.8. [KB1]"
    )


def test_verified_negative_result_keeps_sign_in_currency_and_unicode_rendering():
    ledger = EvidenceLedger(required={"kb"})
    ledger.add("kb", [{"content": "new 1136; old 1171"}])
    result = calculate(
        ledger.entries, "subtract", operand(1136, "new 1136"), operand(1171, "old 1171")
    )
    ledger.add("tool", result)
    assert not ledger.issues("Change is −$35 million. [KB1]")
    assert not ledger.issues("Change is -$35.00 million. [KB1]")
    assert not ledger.issues("A decrease of $35 million. Formula 1136 - 1171 = -35. [KB1]")
    assert not ledger.issues("下降35 million，结果-35。 [KB1]")
    assert ledger.issues("Change is +35 million. [KB1]")
    assert ledger.issues("A decrease of $36 million. [KB1]")


def test_arbitrary_calculation_cannot_relabel_the_verified_business_total():
    ledger = EvidenceLedger(required={"db", "db_total"})
    ledger.add("db", [{"total_amount": 300}], sql="SELECT SUM(amount) AS total_amount FROM sales")
    result = calculate(
        ledger.entries, "add", operand(300, "300", "DB1"), operand(300, "300", "DB1")
    )
    ledger.add("tool", result)
    assert ledger.issues("总金额600元。")
    assert not ledger.issues("总金额300元。")


def test_calculator_tool_is_registered_and_available_for_knowledge_only_tasks():
    import asyncio
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    from src.agent.agent import AgenticRunner
    from src.agent.metric_results import prepare_business_tools

    agent = AgenticRunner()._build_agent(initial_model=TestModel())
    assert "calculate_evidence_arithmetic" in agent._function_toolset.tools
    ledger = EvidenceLedger(required={"kb"}, prompt="仅查询知识库文档，不查询业务数据库")
    tools = [
        SimpleNamespace(name=name)
        for name in (
            "search_knowledge_documents",
            "calculate_evidence_arithmetic",
            "query_database",
            "inspect_local_file",
        )
    ]
    exposed = asyncio.run(
        prepare_business_tools(SimpleNamespace(deps=SimpleNamespace(ledger=ledger)), tools)
    )
    assert {t.name for t in exposed} == {"search_knowledge_documents"}
    ledger.add("kb", [{"content": "value 10"}])
    exposed = asyncio.run(
        prepare_business_tools(SimpleNamespace(deps=SimpleNamespace(ledger=ledger)), tools)
    )
    assert {t.name for t in exposed} == {
        "search_knowledge_documents",
        "calculate_evidence_arithmetic",
    }
