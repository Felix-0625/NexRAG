"""Check all planned acceptance groups, missing evidence, and independence."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path

from evals.quality.metrics import finite_number
from evals.quality.records import read_jsonl

PROTOCOL_PATH = Path(__file__).with_name("protocol-v1.json")
QUALITY_GROUPS = ("zh-general", "en-medical", "en-legal")
PERFORMANCE = {"retrieval_p95_seconds", "e2e_p95_seconds"}
BINARY = {"hallucination_rate", "groundedness", "citation_accuracy"}
SAFETY = {
    "cross_tenant",
    "revocation",
    "privacy",
    "prompt_injection",
    "medical_harm",
    "legal_false_authority",
}


def comparison(value: float, operator: str, target: float) -> bool:
    """Apply the frozen threshold comparator to the observed value."""
    if operator == ">":
        return value > target
    if operator == "<":
        return value < target
    if operator == "=":
        return value == target
    raise ValueError("Unsupported frozen comparator")


def quality_result(
    metric: str, rows: list[dict], planned: int, threshold: list, minimum: int
) -> dict:
    """Summarize metric evidence without treating missing scores as passes."""
    values = []
    errors = 0
    groups = set()
    for row in rows:
        value = row.get("scores", {}).get(metric)
        valid = finite_number(value) or (metric in BINARY and isinstance(value, bool))
        valid = valid and 0 <= value <= 1 and (metric not in BINARY or value in (0, 1))
        if valid:
            values.append(float(value))
            if row.get("source_group"):
                groups.add(row["source_group"])
        elif metric in row.get("errors", {}):
            errors += 1
    value = statistics.mean(values) if values else None
    status = "unmeasured"
    if values:
        status = "passed" if comparison(value, *threshold) else "failed"
        if len(values) != planned or len(groups) < minimum or planned < minimum:
            status = "insufficient_evidence"
    if errors:
        status = "scorer_error"
    return {
        "metric": metric,
        "threshold": threshold,
        "value": value,
        "status": status,
        "planned": planned,
        "scored": len(values),
        "missing": planned - len(values),
        "scorer_errors": errors,
        "independent_source_groups": len(groups),
    }


def assess(
    manifest: dict,
    observations: list[dict],
    *,
    performance: dict | None = None,
    human: dict | None = None,
    safety: dict | None = None,
) -> dict:
    """Assess frozen quality, performance, review, and safety requirements."""
    protocol = json.loads(PROTOCOL_PATH.read_text("utf-8"))
    blockers = []
    _check_manifest(manifest, observations, protocol, blockers)
    thresholds = protocol["thresholds"]
    minimum = protocol["reporting"]["quality_minimum_independent_cases_per_required_group"]
    results = []
    _check_quality_groups(manifest, observations, protocol, thresholds, minimum, results, blockers)
    _check_performance(performance, protocol, thresholds, results)
    _check_human_review(human, minimum, thresholds, results)
    _check_safety(safety, blockers)
    passed = not blockers and all(r["status"] == "passed" for r in results)
    return {
        "protocol": protocol["id"],
        "passed": passed,
        "metrics": results,
        "blockers": blockers,
        "status_counts": dict(Counter(r["status"] for r in results)),
    }


def main() -> None:
    """Run the command-line workflow with the supplied options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--performance", type=Path)
    parser.add_argument("--human", type=Path)
    parser.add_argument("--safety", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def read(path):
        """Handle read for this workflow."""
        return json.loads(path.read_text("utf-8")) if path else None

    result = assess(
        read(args.manifest),
        read_jsonl(args.observations),
        performance=read(args.performance),
        human=read(args.human),
        safety=read(args.safety),
    )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "status_counts": result["status_counts"],
                "blockers": len(result["blockers"]),
            }
        ),
        flush=True,
    )
    raise SystemExit(0 if result["passed"] else 1)


def _check_safety(safety, blockers):
    for check in sorted(SAFETY):
        row = (safety or {}).get(check, {})
        if row.get("origin") != "real-http-security-test" or row.get("passed") is not True:
            blockers.append(
                {
                    "kind": "safety_check_not_passed",
                    "check": check,
                    "status": "failed" if row.get("passed") is False else "unmeasured",
                }
            )


def _check_human_review(human, minimum, thresholds, results):
    review = human or {}
    human_valid = (
        review.get("origin") == "independent-human"
        and review.get("scope") == "this-run"
        and review.get("blinded") is True
        and review.get("reviewed", 0) >= minimum
        and len(set(review.get("reviewer_ids", []))) >= 2
        and finite_number(review.get("agreement"))
        and 0 <= review["agreement"] <= 1
    )
    agreement = review.get("agreement") if human_valid else None
    results.append(
        {
            "group": "independent-review",
            "metric": "human_judge_agreement",
            "value": agreement,
            "threshold": thresholds["human_judge_agreement"],
            "status": (
                "passed"
                if comparison(agreement, *thresholds["human_judge_agreement"])
                else "failed"
            )
            if human_valid
            else "unmeasured",
        }
    )


def _check_performance(performance, protocol, thresholds, results):
    for metric in sorted(PERFORMANCE):
        endpoint = "retrieval" if metric.startswith("retrieval") else "e2e"
        for concurrency in protocol["performance"]["concurrency"]:
            row = (performance or {}).get(f"{endpoint}/{concurrency}", {})
            value = row.get("p95_seconds")
            valid = (
                row.get("kind") == "actual_http_load"
                and row.get("evidence_complete") is True
                and row.get("planned", 0) >= 1000
                and row.get("error_rate") == 0
                and row.get("replay_or_query_cache") is False
                and finite_number(value)
                and value >= 0
            )
            status = (
                "passed"
                if valid and comparison(value, *thresholds[metric])
                else ("failed" if valid else "insufficient_evidence" if row else "unmeasured")
            )
            results.append(
                {
                    "group": f"concurrency-{concurrency}",
                    "metric": metric,
                    "value": value,
                    "threshold": thresholds[metric],
                    "status": status,
                }
            )


def _check_quality_groups(manifest, observations, protocol, thresholds, minimum, results, blockers):
    for group in QUALITY_GROUPS:
        expected = manifest.get("case_ids", {}).get(group, [])
        if len(set(expected)) != len(expected) or not expected:
            blockers.append({"kind": "missing_or_duplicate_group_plan", "group": group})
        rows = [
            r
            for r in observations
            if r.get("cohort") == group
            and r.get("id") in expected
            and r.get("split") == "holdout"
            and r.get("receipt_kind") == "real-native-http"
        ]
        # Duplicate receipts are never used twice, even when they would improve
        # the mean or pretend to increase the independent sample size.
        rows = list({r["id"]: r for r in rows}.values())
        for metric, threshold in thresholds.items():
            if metric in PERFORMANCE or metric == "human_judge_agreement":
                continue
            result = quality_result(metric, rows, len(expected), threshold, minimum)
            results.append({"group": group, **result})
        answered = sum(r.get("answered") is True for r in rows)
        completion = answered / len(expected) if expected else None
        if (
            completion is None
            or completion < protocol["generation"]["minimum_answerable_completion_rate"]
        ):
            blockers.append(
                {
                    "kind": "answerable_completion_below_minimum",
                    "group": group,
                    "value": completion,
                    "planned": len(expected),
                }
            )


def _check_manifest(manifest, observations, protocol, blockers):
    required = protocol["reporting"]["required_manifest"]
    missing = [key for key in required if key not in manifest]
    if missing:
        blockers.append({"kind": "incomplete_frozen_manifest", "fields": missing})
    if manifest.get("protocol_sha256") != hashlib.sha256(PROTOCOL_PATH.read_bytes()).hexdigest():
        blockers.append({"kind": "protocol_hash_mismatch"})
    if manifest.get("provenance") != "real-native-http" or not manifest.get("independent_holdout"):
        blockers.append({"kind": "release_independence_not_established"})
    if manifest.get("k") != 10:
        blockers.append({"kind": "wrong_k"})
    identities = [r.get("id") for r in observations]
    duplicates = [key for key, count in Counter(identities).items() if count > 1]
    if duplicates:
        blockers.append({"kind": "duplicate_case_receipts", "ids": duplicates})


if __name__ == "__main__":
    main()
