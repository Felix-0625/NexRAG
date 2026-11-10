"""严格门禁不能把高分、重复样本、模拟人工或缺失分组视作完成。"""

from evals.quality.gate import assess, quality_result


def rows(value=1, count=300, *, shared_source=False):
    return [
        {
            "id": str(i),
            "source_group": "same" if shared_source else str(i),
            "scores": {"recall_at_10": value},
        }
        for i in range(count)
    ]


def test_strict_threshold_does_not_accept_equality():
    result = quality_result("recall_at_10", rows(0.90), 300, [">", 0.90], 300)
    assert result["value"] == 0.90
    assert result["status"] == "failed"


def test_missing_and_correlated_cases_keep_high_scores_from_passing():
    missing = quality_result("recall_at_10", rows(count=299), 300, [">", 0.90], 300)
    assert missing["value"] == 1 and missing["missing"] == 1
    assert missing["status"] == "insufficient_evidence"
    correlated = quality_result(
        "recall_at_10", rows(count=600, shared_source=True), 600, [">", 0.90], 300
    )
    assert correlated["status"] == "insufficient_evidence"
    assert correlated["independent_source_groups"] == 1


def test_hallucination_is_answer_level_and_scorer_errors_are_not_discarded():
    invalid = [{"id": "x", "source_group": "s", "scores": {"hallucination_rate": 0.005}}]
    result = quality_result("hallucination_rate", invalid, 1, ["<", 0.01], 1)
    assert result["status"] == "unmeasured"
    failed = [{"id": "x", "errors": {"faithfulness": "judge parse error"}}]
    assert quality_result("faithfulness", failed, 1, [">", 0.95], 1)["status"] == "scorer_error"


def test_imitation_and_external_calibration_never_count_as_this_run_human_review():
    for origin, scope in (("ai-imitation", "this-run"), ("independent-human", "external-ragtruth")):
        report = assess(
            {},
            [],
            human={
                "origin": origin,
                "scope": scope,
                "blinded": True,
                "reviewed": 1000,
                "reviewer_ids": ["a", "b"],
                "agreement": 1.0,
            },
        )
        assert report["passed"] is False
        human = next(m for m in report["metrics"] if m["metric"] == "human_judge_agreement")
        assert human["status"] == "unmeasured" and human["value"] is None
        assert len([m for m in report["metrics"] if m["metric"] == "recall_at_10"]) == 3


def test_invalid_agreement_and_negative_latency_do_not_pass():
    report = assess(
        {},
        [],
        human={
            "origin": "independent-human",
            "scope": "this-run",
            "blinded": True,
            "reviewed": 300,
            "reviewer_ids": ["a", "b"],
            "agreement": 100,
        },
        performance={
            "retrieval/1": {
                "kind": "actual_http_load",
                "evidence_complete": True,
                "planned": 1000,
                "error_rate": 0,
                "replay_or_query_cache": False,
                "p95_seconds": -1,
            }
        },
    )
    human = next(m for m in report["metrics"] if m["metric"] == "human_judge_agreement")
    assert human["status"] == "unmeasured"
    latency = next(
        m
        for m in report["metrics"]
        if m["metric"] == "retrieval_p95_seconds" and m["group"] == "concurrency-1"
    )
    assert latency["status"] == "insufficient_evidence"
