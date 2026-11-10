"""正式验收统计不得隐藏截断、重复、缺失或 HTTP 故障。"""

import math

import pytest

from evals.quality.metrics import http_summary, ranking_metrics


def test_ranking_uses_all_official_positives_and_does_not_reward_duplicates():
    value = ranking_metrics(["noise", "a", "a", "b"], {"a": 2, "b": 1, "missing": 1})
    assert value["recall_at_10"] == pytest.approx(2 / 3)
    assert value["mrr"] == 0.5
    assert value["ndcg_at_10"] == pytest.approx(
        (3 / math.log2(3) + 1 / math.log2(5)) / (3 + 1 / math.log2(3) + 1 / math.log2(4))
    )


def test_formal_ranking_rejects_a_different_k_and_vacuous_no_answer_score():
    with pytest.raises(ValueError, match="10"):
        ranking_metrics([str(i) for i in range(16)], {"1": 1})
    with pytest.raises(ValueError, match="No-answer"):
        ranking_metrics([], {"irrelevant": 0})
    assert ranking_metrics([], {"relevant": 1}) == {"recall_at_10": 0, "mrr": 0, "ndcg_at_10": 0}


def test_http_errors_and_missing_attempts_block_complete_evidence():
    rows = [
        {"request_id": "a", "seconds": 0.1, "ok": True},
        {"request_id": "b", "seconds": 30, "ok": False},
    ]
    measured = http_summary(rows, expected=2, wall_seconds=31)
    assert measured["p95_seconds"] == 30
    assert measured["successful_only_p95_seconds"] == 0.1
    assert measured["error_rate"] == 0.5
    assert measured["evidence_complete"] is False
    incomplete = http_summary(rows, expected=3, wall_seconds=31)
    assert incomplete["p95_seconds"] is None
    assert incomplete["unmeasured"] == 1


def test_http_duplicate_receipts_and_nonfinite_timings_are_rejected():
    row = {"request_id": "a", "seconds": 0.1, "ok": True}
    with pytest.raises(ValueError, match="Duplicate"):
        http_summary([row, row], expected=2, wall_seconds=1)
    with pytest.raises(ValueError, match="Finite"):
        http_summary([dict(row, seconds=float("nan"))], expected=1, wall_seconds=1)
