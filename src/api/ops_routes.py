"""Operations API for traces, evaluations, proposals, and feedback.

Management requires a signed administrator identity; only answer owners may submit feedback.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.dependencies import require_admin
from src.evaluation import datasets
from src.evolution.artifacts import registry
from src.evolution.cycle import EvolutionCycle
from src.evolution.governance import Governance, GovernanceError
from src.midplatform.ops_store import get_ops_store, now
from src.midplatform.tenant_access import Principal, get_principal
from src.observability.tracer import tracer

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/overview")
def overview(days: float = Query(7, gt=0, le=90), _: Principal = Depends(require_admin)):
    """Summarize up to 90 days of traces, latency, scores, trends, backlog, and production
    artifacts.
    """
    store = get_ops_store()
    since = now() - days * 86_400
    totals = (
        store.query_one(
            "SELECT COUNT(*) AS traces, AVG(latency_ms) AS avg_latency, "
            "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, SUM(canary) AS canary "
            "FROM traces WHERE created_at >= ?",
            (since,),
        )
        or {}
    )
    score_means = store.query(
        "SELECT s.name, s.source, AVG(s.value) AS mean, COUNT(*) AS n FROM scores s JOIN traces t "
        "ON t.id = s.trace_id WHERE t.created_at >= ? GROUP BY s.name, s.source",
        (since,),
    )
    daily = store.query(
        "SELECT CAST(created_at / 86400 AS INTEGER) * 86400 AS day, COUNT(*) AS traces, "
        "AVG(latency_ms) AS avg_latency, AVG(CASE WHEN status = 'ok' THEN 1.0 ELSE 0 END) AS success "
        "FROM traces WHERE created_at >= ? GROUP BY day ORDER BY day",
        (since,),
    )
    return {
        "totals": totals,
        "scores": score_means,
        "daily": daily,
        "backlog": Governance().backlog_status(),
        "production": {k: registry.production(k).id for k in ("prompt", "params")},
    }


@router.get("/traces")
def list_traces(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = None,
    principal: Principal = Depends(require_admin),
):
    """List recent traces, exposing input previews only for the administrator's own traces."""
    clause, params = ("WHERE status = ?", (status,)) if status else ("", ())
    from src.observability.redaction import hash_user

    return get_ops_store().query(
        f"SELECT id, user_hash, tenant_id, chat_id, status, latency_ms, canary, created_at, "
        f"CASE WHEN user_hash=? THEN substr(input_text, 1, 160) ELSE NULL END AS input_preview FROM traces {clause} ORDER BY created_at DESC LIMIT ?",
        (hash_user(principal.user_id), *params, limit),
    )


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, principal: Principal = Depends(require_admin)):
    """Return trace steps and scores, redacting other users' content; return 404 if absent."""
    store = get_ops_store()
    trace = store.query_one("SELECT * FROM traces WHERE id = ?", (trace_id,))
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    from src.observability.redaction import hash_user

    if trace["user_hash"] != hash_user(principal.user_id):
        trace["input_text"] = trace["output_text"] = trace["error"] = None
    trace["artifact_versions"] = json.loads(trace["artifact_versions"])
    observations = store.query(
        "SELECT * FROM observations WHERE trace_id = ? ORDER BY position", (trace_id,)
    )
    if trace["user_hash"] != hash_user(principal.user_id):
        for item in observations:
            item["input_summary"] = item["output_summary"] = None
            item["error"] = "tool_execution_error" if item["error"] else None
    return {
        "trace": trace,
        "observations": observations,
        "scores": store.query(
            "SELECT * FROM scores WHERE trace_id = ? ORDER BY created_at", (trace_id,)
        ),
    }


class SyntheticSampleForm(BaseModel):
    """Administrator-curated synthetic evaluation sample."""

    input_text: str = Field(min_length=1, max_length=8_000)
    tenant_id: str = Field(pattern="^synthetic-[ab]$")
    split: str = Field(pattern="^(train|holdout|regression)$")
    expected: dict[str, Any]
    confirmed_synthetic: bool


@router.post("/datasets/synthetic")
def add_synthetic_sample(body: SyntheticSampleForm, _: Principal = Depends(require_admin)):
    """Add confirmed synthetic samples using fixed tenants; reject PII or real-resource references."""
    if not body.confirmed_synthetic or len(json.dumps(body.expected)) > 10_000:
        raise HTTPException(400, "请仅提交人工构造的合成样本和有限的预期结果")
    try:
        identifier = datasets.add_synthetic_item(
            body.input_text, body.tenant_id, body.split, body.expected
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": identifier}


class SyntheticExperimentForm(BaseModel):
    """Bounded local-model synthetic experiment options."""

    model_id: str | None = Field(default=None, max_length=150)
    repeats: int = Field(default=1, ge=1, le=3)


@router.post("/experiments/synthetic")
async def run_synthetic_samples(
    body: SyntheticExperimentForm, _: Principal = Depends(require_admin)
):
    """Evaluate samples in isolation with a registered local model and one to three repeats."""
    from src.evaluation.runner import run_synthetic_experiment

    try:
        result = await run_synthetic_experiment(model_id=body.model_id, repeats=body.repeats)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result.__dict__


@router.get("/datasets")
def list_dataset_items(split: str | None = None, _: Principal = Depends(require_admin)):
    """List evaluation entries, optionally filtered by train, holdout, or regression split."""
    splits = (split,) if split else datasets.SPLITS
    return [item.__dict__ for item in datasets.items(splits)]


@router.get("/proposals")
def list_proposals(status: str | None = None, _: Principal = Depends(require_admin)):
    """List evolution proposals and review backlog, optionally filtered by status."""
    return {"proposals": Governance().list(status), "backlog": Governance().backlog_status()}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, _: Principal = Depends(require_admin)):
    """Return proposal details and live canary metrics, or 404 if absent."""
    proposal = Governance().get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    cycle = EvolutionCycle()
    if proposal["status"] == "canary":
        proposal["canary_metrics"] = cycle.canary_metrics(proposal)
    return proposal


class ReviewBody(BaseModel):
    """Candidate-version confirmation for proposal review."""

    note: str | None = Field(default=None, max_length=1_000)
    candidate_id: str | None = Field(default=None, max_length=80)


def _act(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except GovernanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: str, body: ReviewBody, admin: Principal = Depends(require_admin)):
    """Approve the current candidate for canary testing; require review again if it changed."""
    return _act(
        Governance().approve,
        proposal_id,
        admin.email,
        body.note,
        expected_candidate_id=body.candidate_id,
    )


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: str, body: ReviewBody, admin: Principal = Depends(require_admin)):
    """Reject pending or expired proposals and archive candidates; return 409 for invalid
    transitions.
    """
    return _act(Governance().reject, proposal_id, admin.email, body.note)


@router.post("/proposals/{proposal_id}/promote")
def promote(proposal_id: str, admin: Principal = Depends(require_admin)):
    """Require a second approval for promotion and recompute canary gates server-side."""
    return _act(Governance().promote, proposal_id, admin.email)


@router.post("/proposals/{proposal_id}/rollback")
def rollback(proposal_id: str, body: ReviewBody, admin: Principal = Depends(require_admin)):
    """Stop a canary or restore the predecessor of a promoted version; reject other states with
    409.
    """
    return _act(Governance().rollback, proposal_id, body.note or "manual rollback", admin.email)


@router.post("/evolution/run")
async def run_cycle(_: Principal = Depends(require_admin)):
    """Run a batch evolution cycle that submits reviewed candidates without deploying them."""
    from src.memory.runtime import llm_complete

    report = await EvolutionCycle(completer=llm_complete).run()
    return report.__dict__


class FeedbackBody(BaseModel):
    """User feedback associated with an owned answer."""

    trace_id: str = Field(min_length=1, max_length=80)
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1_000)


@router.post("/feedback")
def feedback(body: FeedbackBody, principal: Principal = Depends(get_principal)):
    """Accept Open WebUI thumbs-up or thumbs-down only from the answer's signed owner."""
    from src.observability.redaction import hash_user

    store = get_ops_store()
    with store.transaction(immediate=True):
        trace = store.query_one("SELECT user_hash FROM traces WHERE id = ?", (body.trace_id,))
        if not trace or trace["user_hash"] != hash_user(principal.user_id):
            raise HTTPException(status_code=404, detail="Trace not found")
        tracer.record_rating(body.trace_id, 1.0 if body.rating > 0 else 0.0, body.comment or None)
    return {"ok": True}
