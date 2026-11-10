"""Proposal lifecycle, backlog, canary routing, promotion, and rollback."""

from __future__ import annotations

import pytest

from src.config import settings
from src.evolution.artifacts import ArtifactRegistry
from src.evolution.governance import Governance, GovernanceError
from src.evolution.routing import in_canary, resolve

DAY = 86_400


@pytest.fixture
def reg(isolated_ops):
    """Provide reg test data or behavior for this scenario."""
    return ArtifactRegistry(isolated_ops)


@pytest.fixture
def gov(isolated_ops, reg):
    """Provide gov test data or behavior for this scenario."""
    return Governance(isolated_ops, reg)


def submit(gov, reg, *, kind="params", cluster="tool_failure:query_database", value=3):
    """Provide submit test data or behavior for this scenario."""
    baseline = reg.production(kind)
    content = (
        {**baseline.content, "prune_max_tables": value} if kind == "params" else "new guidance"
    )
    candidate = reg.create(kind, content, parent_id=baseline.id)
    return gov.submit(
        kind=kind,
        cluster_key=cluster,
        candidate_id=candidate.id,
        baseline_id=baseline.id,
        evidence_count=6,
        report={
            "what_changed": {},
            "effect": {
                "baseline_versions": {
                    kind: reg.production(kind).id for kind in ("prompt", "params")
                }
            },
        },
        priority=1.0,
    ), candidate


def test_same_cluster_merges_and_other_cluster_defers(gov, reg):
    (first, action), previous_candidate = submit(gov, reg)
    assert action == "created"
    (merged, action), _ = submit(gov, reg, value=4)
    assert (merged, action) == (first, "merged")
    with pytest.raises(GovernanceError, match="Proposal changed"):
        gov.approve(first, "admin@example.local", expected_candidate_id=previous_candidate.id)
    (_, action), deferred = submit(gov, reg, cluster="timeout:-")
    assert action == "deferred" and reg.get(deferred.id).label == "rejected"
    assert len(gov.list("pending_review")) == 1


def test_queue_limit(gov, reg, monkeypatch):
    monkeypatch.setattr(settings, "PROPOSAL_QUEUE_LIMIT", 1)
    submit(gov, reg, kind="params")
    (_, action), _ = submit(gov, reg, kind="prompt", cluster="no_evidence:-")
    assert action == "queue_full"


def test_baseline_change_marks_stale_and_blocks_approval(gov, reg):
    (proposal_id, _), _ = submit(gov, reg)
    newer = reg.create("params", {"prune_max_tables": 1}, label="candidate")
    reg.set_label(newer.id, "production")
    with pytest.raises(GovernanceError, match="stale"):
        gov.approve(proposal_id, "admin@example.local")
    assert gov.get(proposal_id)["status"] == "stale"


@pytest.mark.parametrize("effect", [None, {"baseline_versions": {}}])
def test_legacy_report_requires_fresh_evaluation(gov, reg, isolated_ops, effect):
    import json

    (proposal_id, _), _ = submit(gov, reg)
    report = {} if effect is None else {"effect": effect}
    isolated_ops.execute(
        "UPDATE proposals SET report = ? WHERE id = ?", (json.dumps(report), proposal_id)
    )
    with pytest.raises(GovernanceError, match="refreshed"):
        gov.approve(proposal_id, "admin@example.local")
    assert gov.get(proposal_id)["status"] == "stale"


def test_other_artifact_change_invalidates_evaluation(gov, reg):
    (proposal_id, _), _ = submit(gov, reg)
    newer = reg.create("prompt", "Updated production guidance")
    reg.set_label(newer.id, "production")
    with pytest.raises(GovernanceError, match="refreshed"):
        gov.approve(proposal_id, "admin@example.local")


def test_ttl_expiry(gov, reg, isolated_ops, monkeypatch):
    (proposal_id, _), candidate = submit(gov, reg)
    isolated_ops.execute(
        "UPDATE proposals SET created_at = created_at - ? WHERE id = ?",
        ((settings.PROPOSAL_TTL_DAYS + 1) * DAY, proposal_id),
    )
    assert gov.housekeeping()["expired"] == 1
    assert gov.get(proposal_id)["status"] == "expired" and reg.get(candidate.id).label == "rejected"


def test_approve_canary_promote_then_rollback(gov, reg, isolated_ops, monkeypatch):
    original = reg.production("params")
    (proposal_id, _), candidate = submit(gov, reg)
    gov.approve(proposal_id, "admin@example.local", "looks right")
    assert reg.by_label("params", "staging").id == candidate.id
    assert reg.production("params").id == original.id  # Approval alone must not promote.

    with pytest.raises(GovernanceError, match="Canary has run"):
        gov.promote(proposal_id, "admin@example.local")
    isolated_ops.execute(
        "UPDATE proposals SET canary_started_at = canary_started_at - ? WHERE id = ?",
        ((settings.CANARY_MIN_DAYS + 1) * DAY, proposal_id),
    )
    from src.midplatform.ops_store import new_id
    from src.observability.tracer import Tracer, TraceRecord

    recorder = Tracer(isolated_ops)
    for index in range(40):
        recorder.record(
            TraceRecord(
                new_id(),
                "test",
                "t1",
                None,
                "synthetic",
                artifact_versions={"params": candidate.id if index % 2 else original.id},
            ),
            output_text="ok",
        )
    gov.promote(proposal_id, "admin@example.local")
    assert reg.production("params").id == candidate.id

    gov.rollback(proposal_id, "online regression")
    assert reg.production("params").id == original.id
    assert gov.get(proposal_id)["status"] == "rolled_back"


def test_canary_routing_is_stable_per_user(reg, monkeypatch):
    monkeypatch.setattr(settings, "CANARY_RATIO", 0.5)
    users = [f"user-{i}" for i in range(200)]
    canary_users = [u for u in users if in_canary(u)]
    assert 60 < len(canary_users) < 140
    staging = reg.create("params", {"prune_max_tables": 4})
    reg.set_label(staging.id, "staging")
    assert resolve(canary_users[0], reg).versions["params"] == staging.id
    assert resolve(next(u for u in users if not in_canary(u)), reg).canary is False
    assert resolve(canary_users[0], reg).versions == resolve(canary_users[0], reg).versions


def test_rollback_of_old_proposal_keeps_new_production(gov, reg, isolated_ops):
    (proposal_id, _), candidate = submit(gov, reg)
    isolated_ops.execute("UPDATE proposals SET status = 'promoted' WHERE id = ?", (proposal_id,))
    reg.set_label(candidate.id, "production")
    newer = reg.create("params", {"prune_max_tables": 4}, parent_id=candidate.id)
    reg.set_label(newer.id, "production")

    with pytest.raises(GovernanceError, match="current production"):
        gov.rollback(proposal_id, "stale browser tab")
    assert reg.production("params").id == newer.id
    assert gov.get(proposal_id)["status"] == "promoted"


def test_approval_rolls_back_if_staging_label_fails(gov, reg, monkeypatch):
    (proposal_id, _), _ = submit(gov, reg)

    def fail(*_args):
        """Provide fail test data or behavior for this scenario."""
        raise ValueError("staging write failed")

    monkeypatch.setattr(reg, "set_label", fail)
    with pytest.raises(ValueError, match="staging write failed"):
        gov.approve(proposal_id, "admin@example.local")
    assert gov.get(proposal_id)["status"] == "pending_review"
    assert reg.by_label("params", "staging") is None


def test_canary_cannot_promote_with_no_real_samples_or_forged_summary(gov, reg, isolated_ops):
    (proposal_id, _), candidate = submit(gov, reg)
    original = reg.production("params").id
    gov.approve(proposal_id, "admin@example.local")
    isolated_ops.execute(
        "UPDATE proposals SET canary_started_at=canary_started_at-? WHERE id=?",
        ((settings.CANARY_MIN_DAYS + 1) * DAY, proposal_id),
    )
    with pytest.raises(TypeError):  # Callers cannot supply fabricated canary summaries.
        gov.promote(proposal_id, "admin@example.local", canary_metrics={"canary_traces": 1000})
    with pytest.raises(GovernanceError, match="20 candidate"):
        gov.promote(proposal_id, "admin@example.local")
    assert reg.production("params").id == original
    assert reg.by_label("params", "staging").id == candidate.id
