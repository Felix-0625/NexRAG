"""End-to-end evolution with deterministic tasks instead of real models."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from src.config import settings
from src.evaluation import datasets
from src.evaluation.evaluators import RunOutcome
from src.evaluation.synthetic import record_synthetic_failure
from src.evolution.artifacts import ArtifactRegistry
from src.evolution.cycle import EvolutionCycle
from src.evolution.governance import Governance
from src.evolution.miner import FailureCluster, mine_failures
from src.evolution.proposer import cluster_evidence
from src.midplatform.ops_store import dumps, new_id, now
from src.observability.tracer import Tracer, TraceRecord


def seed_items(store):
    """Provide seed items test data or behavior for this scenario."""
    rows = [("train", "Q-train"), ("holdout", "Q-holdout"), ("regression", "Q-regression")]
    for split, question in rows:
        store.execute(
            "INSERT INTO dataset_items VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                "nexrag",
                split,
                "business_metric",
                question,
                dumps({"tenant_id": "t1", "answer_contains": ["42"]}),
                None,
                now(),
            ),
        )


def seed_failures(store, n=6):
    """Provide seed failures test data or behavior for this scenario."""
    for index in range(n):
        datasets.add_synthetic_item(
            f"Count the orders in synthetic case {index}",
            "synthetic-a",
            "train",
            {"required_tools": ["query_database"]},
            store=store,
        )
    for item in datasets.items(("train",), store):
        record_synthetic_failure(item, RunOutcome("", synthetic_fixture=True), 0.0, store)


def task_answers_with_more_tables(seen: list[str]):
    """Simulate improvement by succeeding only with prune_max_tables at least three."""

    async def task(item, guidance, params):
        """Provide task test data or behavior for this scenario."""
        seen.append(item.split)
        good = params.get("prune_max_tables", 2) >= 3
        return RunOutcome(
            answer="42" if good else "unknown", tools=["query_database"], latency_ms=100
        )

    return task


def test_cycle_creates_gated_proposal_with_change_report(isolated_ops, monkeypatch):
    monkeypatch.setattr(settings, "EVOLUTION_REPEATS", 1)
    seed_items(isolated_ops)
    seed_failures(isolated_ops)
    seen: list[str] = []
    reg = ArtifactRegistry(isolated_ops)
    cycle = EvolutionCycle(task=task_answers_with_more_tables(seen), store=isolated_ops, reg=reg)

    report = asyncio.run(cycle.run())

    params_action = next(a for a in report.actions if a.get("kind") == "params")
    assert params_action["action"] == "created"
    proposal = Governance(isolated_ops, reg).get(params_action["proposal_id"])
    change = proposal["report"]
    assert change["what_changed"]["params"]["prune_max_tables"] == {"from": 2, "to": 3}
    assert change["why"]["cluster"] == "tool_failure:query_database"
    assert (
        change["effect"]["candidate"]["holdout"]["quality"]
        > change["effect"]["baseline"]["holdout"]["quality"]
    )
    assert all(check["passed"] for check in change["gate"])
    assert change["rollback"]["to_version"] == reg.production("params").id
    # Production stays unchanged until human approval and promotion.
    assert reg.production("params").content["prune_max_tables"] == 2


def test_parameter_search_never_sees_holdout(isolated_ops, monkeypatch):
    monkeypatch.setattr(settings, "EVOLUTION_REPEATS", 1)
    seed_items(isolated_ops)
    seed_failures(isolated_ops)
    seen: list[str] = []
    cycle = EvolutionCycle(
        task=task_answers_with_more_tables(seen),
        store=isolated_ops,
        reg=ArtifactRegistry(isolated_ops),
    )
    asyncio.run(
        cycle._best_params(
            cycle.reg.production("params"),
            mine_failures(isolated_ops)[0],
            datasets.items(("train",), isolated_ops),
            cycle.reg.production("prompt"),
        )
    )
    assert set(seen) == {"train"}


def test_small_clusters_do_not_trigger_changes(isolated_ops):
    seed_items(isolated_ops)
    seed_failures(isolated_ops, n=2)
    cycle = EvolutionCycle(
        task=task_answers_with_more_tables([]),
        store=isolated_ops,
        reg=ArtifactRegistry(isolated_ops),
    )
    report = asyncio.run(cycle.run())
    assert report.actions == [{"action": "none", "detail": "no cluster reached the minimum size"}]


def test_held_out_failures_stay_out_of_mining_and_prompt_evidence(isolated_ops):
    outcome = RunOutcome("", synthetic_fixture=True)
    legacy_trace_ids = []
    for split in ("holdout", "regression"):
        datasets.add_synthetic_item(
            f"Independent {split} case",
            "synthetic-a",
            split,
            {"answer_contains": ["42"]},
            store=isolated_ops,
        )
        item = datasets.items((split,), isolated_ops)[0]
        assert record_synthetic_failure(item, outcome, 0.0, isolated_ops) is None
        # Reproduce old shared traces incorrectly populated with holdout failures.
        legacy_trace_ids.append(
            record_synthetic_failure(replace(item, split="train"), outcome, 0.0, isolated_ops)
        )
    assert all(legacy_trace_ids)
    assert mine_failures(isolated_ops) == []
    assert (
        cluster_evidence(
            FailureCluster("legacy", "evaluation_failure", None, legacy_trace_ids), isolated_ops
        )
        == []
    )


def test_canary_auto_rollback_on_online_drop(isolated_ops):
    reg = ArtifactRegistry(isolated_ops)
    gov = Governance(isolated_ops, reg)
    base = reg.production("params")
    cand = reg.create("params", {**base.content, "knowledge_top_k": 8}, parent_id=base.id)
    proposal_id, _ = gov.submit(
        kind="params",
        cluster_key="k",
        candidate_id=cand.id,
        baseline_id=base.id,
        evidence_count=6,
        report={
            "effect": {"baseline_versions": {k: reg.production(k).id for k in ("prompt", "params")}}
        },
        priority=1,
    )
    gov.approve(proposal_id, "admin@example.local")
    tracer = Tracer(isolated_ops)
    for i in range(40):
        canary = i % 2 == 0
        trace = TraceRecord(
            new_id(),
            f"u{i}",
            "t1",
            None,
            "q",
            canary=canary,
            artifact_versions={"params": cand.id if canary else base.id},
        )
        tracer.record(trace, output_text="", status="error" if canary else "ok")
    states = EvolutionCycle(store=isolated_ops, reg=reg).monitor_canaries()
    assert states[0]["state"] == "rolled_back"
    assert reg.by_label("params", "staging") is None
    assert json.loads(json.dumps(states))  # Keep dashboard reports serializable.


def test_unrelated_version_traffic_does_not_count_as_canary_evidence(isolated_ops):
    reg = ArtifactRegistry(isolated_ops)
    gov = Governance(isolated_ops, reg)
    base = reg.production("params")
    candidate = reg.create("params", {**base.content, "knowledge_top_k": 2}, parent_id=base.id)
    key, _ = gov.submit(
        kind="params",
        cluster_key="synthetic",
        candidate_id=candidate.id,
        baseline_id=base.id,
        evidence_count=5,
        report={
            "effect": {"baseline_versions": {k: reg.production(k).id for k in ("prompt", "params")}}
        },
        priority=1,
    )
    gov.approve(key, "admin@example.local")
    recorder = Tracer(isolated_ops)
    for index in range(40):
        recorder.record(
            TraceRecord(
                new_id(),
                "other",
                "t1",
                None,
                "unrelated",
                canary=bool(index % 2),
                artifact_versions={"params": "different-version"},
            ),
            output_text="ok",
        )
    metrics = EvolutionCycle(store=isolated_ops, reg=reg).canary_metrics(gov.get(key))
    assert metrics["canary_traces"] == metrics["control_traces"] == 0
