"""Fixed ranking metrics and HTTP statistics retaining all failed requests."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real


def finite_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def ranking_metrics(returned: Sequence[str], qrels: Mapping[str, int]) -> dict[str, float]:
    """Score official K=10 outputs; duplicates retain rank positions but earn no extra credit.

    qrels must use the dataset's official relevant units, not easier file-level hits.
    MRR covers the ten units actually returned, not rankings over the entire corpus.
    """
    if len(returned) > 10:
        raise ValueError("Formal ranking receipt must contain at most 10 units")
    if not qrels or any(type(grade) is not int or grade < 0 for grade in qrels.values()):
        raise ValueError("Official nonnegative integer qrels are required")
    relevant = {key for key, grade in qrels.items() if grade > 0}
    if not relevant:
        raise ValueError("No-answer cases require their own group, not vacuous ranking scores")
    seen: set[str] = set()
    gains = []
    reciprocal_rank = 0.0
    for rank, key in enumerate(returned, 1):
        grade = 0 if key in seen else qrels.get(key, 0)
        seen.add(key)
        gains.append((2**grade - 1) / math.log2(rank + 1))
        if grade and reciprocal_rank == 0:
            reciprocal_rank = 1 / rank
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(sorted(qrels.values(), reverse=True)[:10], 1)
    )
    return {
        "recall_at_10": len(seen & relevant) / len(relevant),
        "mrr": reciprocal_rank,
        "ndcg_at_10": sum(gains) / ideal,
    }


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values or not 0 < percentile <= 1 or any(not finite_number(v) or v < 0 for v in values):
        raise ValueError("Finite nonnegative observations and a valid percentile are required")
    return sorted(values)[math.ceil(percentile * len(values)) - 1]


def http_summary(rows: Sequence[dict], *, expected: int, wall_seconds: float) -> dict:
    """Count all attempts; successful-response P95 must not hide errors or missing responses."""
    if expected < 1 or not finite_number(wall_seconds) or wall_seconds <= 0:
        raise ValueError("Expected count and actual elapsed wall time are required")
    ids = [row["request_id"] for row in rows]
    if len(set(ids)) != len(ids) or len(rows) > expected:
        raise ValueError("Duplicate or excess request receipts")
    complete = [r for r in rows if r.get("ok") is True]
    times = [r["seconds"] for r in rows]
    success_times = [r["seconds"] for r in complete]
    missing = expected - len(rows)
    return {
        "planned": expected,
        "observed": len(rows),
        "successes": len(complete),
        "failed": len(rows) - len(complete),
        "unmeasured": missing,
        "error_rate": (expected - len(complete)) / expected,
        "p95_seconds": nearest_rank(times, 0.95) if times and not missing else None,
        "p99_seconds": nearest_rank(times, 0.99) if times and not missing else None,
        "successful_only_p95_seconds": nearest_rank(success_times, 0.95) if success_times else None,
        "completed_qps": len(complete) / wall_seconds,
        "wall_seconds": wall_seconds,
        "evidence_complete": len(complete) == expected,
    }
