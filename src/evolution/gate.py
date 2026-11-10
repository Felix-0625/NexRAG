"""Require every evaluation gate before a candidate may enter review.

All splits must be safe, baseline regression successes must remain, holdout quality must
gain EVOLUTION_MIN_GAIN, other metric drops must stay within EVOLUTION_MAX_REGRESSION,
and latency growth must stay within EVOLUTION_MAX_LATENCY_GROWTH.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config import settings
from src.evaluation.runner import ExperimentResult

LOWER_IS_BETTER = {"latency_ms", "tool_error_rate", "safety_violation"}
PASS_THRESHOLD = 0.999


@dataclass
class GateVerdict:
    """Accumulated checks and the candidate's overall gate verdict."""

    passed: bool
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> None:
        """Append a check; any failed check makes the overall verdict fail."""
        self.checks.append({"check": name, "passed": ok, "detail": detail})
        if not ok:
            self.passed = False


def evaluate_gate(
    baseline: ExperimentResult,
    candidate: ExperimentResult,
    *,
    regression_item_ids: list[str],
) -> GateVerdict:
    """Compare coverage, safety, regression, holdout gain, metric drops, and latency gates."""
    verdict = GateVerdict(passed=True)
    same_items = (
        bool(baseline.item_scores) and baseline.item_scores.keys() == candidate.item_scores.keys()
    )
    required_splits = {"train", "holdout", "regression"}
    complete = (
        required_splits <= baseline.metrics.keys() and required_splits <= candidate.metrics.keys()
    )
    covered_regressions = (
        bool(regression_item_ids) and set(regression_item_ids) <= candidate.item_scores.keys()
    )
    verdict.add(
        "coverage",
        same_items and complete and covered_regressions,
        "baseline/candidate must cover the same items and all required dataset splits",
    )

    worst = max((m.get("safety_violation", 0.0) for m in candidate.metrics.values()), default=0.0)
    verdict.add("safety", worst == 0.0, f"max safety_violation={worst:.3f} (must be 0)")

    newly_failing = [
        item_id
        for item_id in regression_item_ids
        if baseline.item_scores.get(item_id, {}).get("quality", 0) >= PASS_THRESHOLD
        and candidate.item_scores.get(item_id, {}).get("quality", 0) < PASS_THRESHOLD
    ]
    verdict.add(
        "regression", not newly_failing, f"{len(newly_failing)} regression item(s) newly failing"
    )

    base_hold = baseline.metrics.get("holdout", {})
    cand_hold = candidate.metrics.get("holdout", {})
    gain = cand_hold.get("quality", 0.0) - base_hold.get("quality", 0.0)
    verdict.add(
        "holdout_gain",
        gain >= settings.EVOLUTION_MIN_GAIN,
        f"holdout quality {base_hold.get('quality', 0):.3f} -> {cand_hold.get('quality', 0):.3f} "
        f"(gain {gain:+.3f}, need >= {settings.EVOLUTION_MIN_GAIN})",
    )

    regressed = []
    for metric, base_value in base_hold.items():
        if metric in {"quality", "n", "latency_ms"} or metric not in cand_hold:
            continue
        delta = cand_hold[metric] - base_value
        if (metric in LOWER_IS_BETTER and delta > settings.EVOLUTION_MAX_REGRESSION) or (
            metric not in LOWER_IS_BETTER and -delta > settings.EVOLUTION_MAX_REGRESSION
        ):
            regressed.append(f"{metric} {base_value:.3f}->{cand_hold[metric]:.3f}")
    verdict.add(
        "no_metric_regression", not regressed, ", ".join(regressed) or "no metric regressed"
    )

    base_latency = base_hold.get("latency_ms", 0.0)
    cand_latency = cand_hold.get("latency_ms", 0.0)
    growth = (cand_latency - base_latency) / base_latency if base_latency else 0.0
    verdict.add(
        "latency",
        growth <= settings.EVOLUTION_MAX_LATENCY_GROWTH,
        f"latency {base_latency:.0f}ms -> {cand_latency:.0f}ms ({growth:+.0%})",
    )
    return verdict
