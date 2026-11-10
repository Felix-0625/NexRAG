"""Every regression category must block candidate promotion."""

from __future__ import annotations

import pytest

from src.evaluation.runner import ExperimentResult
from src.evolution.gate import evaluate_gate


def result(holdout_quality, *, safety=0.0, latency=1000.0, citation=1.0, items=None):
    """Provide result test data or behavior for this scenario."""
    metrics = {
        "train": {"quality": holdout_quality, "safety_violation": 0.0},
        "holdout": {
            "quality": holdout_quality,
            "safety_violation": safety,
            "latency_ms": latency,
            "citation_hit": citation,
            "n": 4.0,
        },
        "regression": {"quality": 1.0, "safety_violation": 0.0},
    }
    return ExperimentResult("run", metrics, items or {"r1": {"quality": 1.0}})


def checks(verdict):
    """Provide checks test data or behavior for this scenario."""
    return {c["check"]: c["passed"] for c in verdict.checks}


def test_clear_improvement_passes():
    verdict = evaluate_gate(result(0.70), result(0.80), regression_item_ids=["r1"])
    assert verdict.passed


@pytest.mark.parametrize(
    ("candidate", "failed_check"),
    [
        (result(0.71), "holdout_gain"),
        (result(0.90, safety=0.25), "safety"),
        (result(0.90, latency=1500.0), "latency"),
        (result(0.90, citation=0.5), "no_metric_regression"),
        (result(0.90, items={"r1": {"quality": 0.0}}), "regression"),
    ],
)
def test_each_regression_class_blocks(candidate, failed_check):
    verdict = evaluate_gate(result(0.70), candidate, regression_item_ids=["r1"])
    assert not verdict.passed
    assert checks(verdict)[failed_check] is False


def test_missing_candidate_cases_cannot_pass_with_good_partial_metrics():
    candidate = result(0.90, items={"different-case": {"quality": 1.0}})
    verdict = evaluate_gate(result(0.70), candidate, regression_item_ids=["r1"])
    assert not verdict.passed and not checks(verdict)["coverage"]
