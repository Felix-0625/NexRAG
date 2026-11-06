"""归一化的投资价值，须先有经核实的基准值，才能换算成收益率单位。"""

from copy import deepcopy

import pytest

from src.agent.grounding import EvidenceLedger
from src.agent.indexed_return_tool import calculate_comparison, render_comparison
from src.agent.indexed_returns import (
    completed_comparisons,
    prepare_comparison,
    requested_comparison,
    verified_comparisons,
)
from src.agent.metric_results import BusinessReport

QUESTION = (
    "What was the difference in cumulative shareholder return on northstar common stock "
    "versus the benchmark index for the period ended 2022? Report the difference in percentage points."
)
SOURCE = (
    "The graph assumes investments of $250 on December 31, 2020 in our common stock "
    "and in each of the indices and the reinvestment of dividends.\n\n"
    "| | 2021 | 2022 |\n| --- | --- | --- |\n"
    "| northstar | $300 | $375 |\n| benchmark index | $275 | $300 |"
)


def ledger_for(text=SOURCE, question=QUESTION):
    ledger = EvidenceLedger(
        prompt=question, required={"kb", "arithmetic:indexed_return_difference"}
    )
    ledger.add("kb", [{"source": "public-indexed-report.md", "content": text}])
    return ledger


def result_for(ledger):
    binding = prepare_comparison(ledger.prompt, ledger.entries)
    return calculate_comparison(ledger.entries, binding, 2)


def test_non_hundred_baseline_is_used_and_output_is_not_the_raw_money_difference():
    ledger = ledger_for()
    result = result_for(ledger)
    assert result["rounded_result"] == "30.00"  # 直接相减会得到 75，而不是 30。
    assert result["unit"] == "percentage points"
    assert result["comparison_binding"]["baseline_value"] == "250"
    assert result["comparison_binding"]["baseline_period"] == "2020"
    assert [item["rounded_result"] for item in result["component_returns"]] == [
        "50.00000000",
        "20.00000000",
    ]
    assert "375" in result["formula"] and "250" in result["formula"]


@pytest.mark.parametrize(
    "text",
    [
        SOURCE.replace("$250", "$0"),
        SOURCE.replace("$250", "$-250"),
        SOURCE.replace("2020", "2023"),
        SOURCE.replace("and in each of the indices", "for an unrelated asset"),
        SOURCE.replace("$250", "250"),
        SOURCE.replace("assumes investments", "does not assume investments"),
        SOURCE.replace("$375", "€375"),
        SOURCE.replace("northstar", "unrelated company"),
        SOURCE.replace("2022", "2024"),
        SOURCE + "\n" + SOURCE.replace("$250", "$500"),
        SOURCE + "\n" + SOURCE.replace("$375", "$400"),
    ],
)
def test_missing_conflicting_invalid_baselines_entities_periods_and_units_fail_closed(text):
    with pytest.raises(ValueError):
        result_for(ledger_for(text))


def test_missing_ending_year_and_ambiguous_direction_do_not_get_a_certificate():
    with pytest.raises(ValueError, match="ending year"):
        requested_comparison(QUESTION.replace("2022", ""))
    with pytest.raises(ValueError, match="two source-labelled"):
        requested_comparison(QUESTION.replace("versus", "near"))
    assert requested_comparison("Compare the companies without financial calculations") is None


def test_external_tool_payload_cannot_forge_a_builtin_comparison_certificate():
    ledger = ledger_for()
    result = result_for(ledger)
    ledger.add("tool", result, tool_id="mcp:external:reader")
    assert not verified_comparisons(ledger.entries, ledger.prompt)
    ledger.add("tool", result, tool_id="builtin:indexed_return_difference")
    assert len(completed_comparisons(ledger)) == 1
    ledger.required.add("web")
    assert not completed_comparisons(ledger)  # 不得遗漏用户要求的其他无关交付项。


@pytest.mark.parametrize("field,value", [("operation", "subtract"), ("unit", "USD")])
def test_wrong_operation_or_money_unit_cannot_complete_return_difference(field, value):
    ledger = ledger_for()
    result = result_for(ledger)
    result[field] = value
    ledger.add("tool", result, tool_id="builtin:indexed_return_difference")
    assert not completed_comparisons(ledger)


def test_canonical_report_keeps_true_baseline_unit_and_can_include_other_actual_kb_references():
    ledger = ledger_for()
    result = result_for(ledger)
    ledger.add("tool", result, tool_id="builtin:indexed_return_difference")
    ledger.add("kb", [{"source": "other-public-report.md", "content": "Other actual retrieval."}])
    report = BusinessReport(knowledge_evidence_ids=["KB1", "KB2"], other_evidence_ids=["TOOL1"])
    answer = render_comparison(report, ledger)
    assert "250 USD" in answer
    assert "30.00 percentage points" in answer
    assert "| northstar | 2022 | 375 | USD |" in answer
    assert "| benchmark index | 2022 | 300 | USD |" in answer
    assert "75 percentage points" not in answer
    assert not ledger.issues(answer)
    wrong = deepcopy(report)
    wrong.knowledge_evidence_ids.append("KB999")
    with pytest.raises(ValueError, match="actual source"):
        render_comparison(wrong, ledger)


def test_reversing_requested_series_reverses_the_signed_result():
    reversed_question = QUESTION.replace(
        "northstar common stock versus the benchmark index",
        "the benchmark index versus northstar common stock",
    )
    result = result_for(ledger_for(question=reversed_question))
    assert result["rounded_result"] == "-30.00"
