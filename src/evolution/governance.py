"""Manage proposal review, canaries, promotion, rollback, and backlog limits.

Pending proposals can enter canary only after approval; promotion requires another review.
Reject, expire, or mark stale proposals as appropriate, and merge repeated cluster evidence.
Allow one open proposal per artifact kind, cap the review queue, and never deploy without
administrator approval.
"""

from __future__ import annotations

import json
from typing import Any

from src.config import settings
from src.evolution.artifacts import KINDS, ArtifactRegistry
from src.evolution.canary import enough_samples, metrics_for_proposal, regressed_metrics
from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now

OPEN = ("pending_review", "stale", "canary")


class GovernanceError(ValueError):
    """Raise for a forbidden proposal lifecycle transition."""


class Governance:
    """Manage lifecycle transitions and artifact writes atomically."""

    def __init__(self, store: OpsStore | None = None, reg: ArtifactRegistry | None = None):
        self._store = store
        self.reg = reg or ArtifactRegistry(store)

    @property
    def store(self) -> OpsStore:
        """Resolve the current operations store lazily."""
        return self._store or get_ops_store()

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        """Return a proposal with its decoded report, or None."""
        row = self.store.query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return {**row, "report": json.loads(row["report"])} if row else None

    def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List proposals by priority within a status, otherwise by recent update, bounded by
        limit.
        """
        if status:
            rows = self.store.query(
                "SELECT * FROM proposals WHERE status = ? ORDER BY priority DESC, created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM proposals ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        return [{**row, "report": json.loads(row["report"])} for row in rows]

    def open_for_kind(self, kind: str) -> dict[str, Any] | None:
        """Return the latest pending, stale, or canary proposal for this artifact kind."""
        marks = ",".join("?" for _ in OPEN)
        row = self.store.query_one(
            f"SELECT * FROM proposals WHERE kind = ? AND status IN ({marks}) ORDER BY created_at DESC LIMIT 1",
            (kind, *OPEN),
        )
        return {**row, "report": json.loads(row["report"])} if row else None

    def queue_size(self) -> int:
        """Count proposals awaiting review."""
        row = self.store.query_one(
            "SELECT COUNT(*) AS n FROM proposals WHERE status = 'pending_review'"
        )
        return int(row["n"]) if row else 0

    def backlog_status(self) -> dict[str, Any]:
        """Summarize backlog counts, limits, age, and half-TTL warnings."""
        counts = {
            row["status"]: row["n"]
            for row in self.store.query(
                "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status"
            )
        }
        oldest = self.store.query_one(
            "SELECT MIN(created_at) AS t FROM proposals WHERE status = 'pending_review'"
        )
        age_days = (now() - oldest["t"]) / 86_400 if oldest and oldest["t"] else 0.0
        return {
            "counts": counts,
            "queue_limit": settings.PROPOSAL_QUEUE_LIMIT,
            "queue_full": counts.get("pending_review", 0) >= settings.PROPOSAL_QUEUE_LIMIT,
            "oldest_pending_days": round(age_days, 1),
            "alert": age_days > settings.PROPOSAL_TTL_DAYS / 2,
        }

    def submit(
        self,
        *,
        kind: str,
        cluster_key: str,
        candidate_id: str,
        baseline_id: str,
        evidence_count: int,
        report: dict[str, Any],
        priority: float,
    ) -> tuple[str, str]:
        """Create or merge a proposal and return its ID and action."""
        with self.store.transaction(immediate=True):
            if baseline_id != self.reg.production(kind).id:
                self.reg.set_label(candidate_id, "rejected")
                return "", "stale"
            existing = self.open_for_kind(kind)
            at = now()
            if (
                existing
                and existing["cluster_key"] == cluster_key
                and existing["status"] != "canary"
            ):
                # Repeated problems refresh existing evidence and candidates instead of multiplying proposals.
                self.reg.set_label(existing["candidate_id"], "rejected")
                self.store.execute(
                    "UPDATE proposals SET candidate_id = ?, baseline_id = ?, evidence_count = ?, report = ?, "
                    "priority = ?, status = 'pending_review', updated_at = ? WHERE id = ?",
                    (
                        candidate_id,
                        baseline_id,
                        evidence_count,
                        dumps(report),
                        priority,
                        at,
                        existing["id"],
                    ),
                )
                return existing["id"], "merged"
            if existing:
                # Do not stack changes when this artifact already has another open proposal.
                self.reg.set_label(candidate_id, "rejected")
                return existing["id"], "deferred"
            if self.queue_size() >= settings.PROPOSAL_QUEUE_LIMIT:
                self.reg.set_label(candidate_id, "rejected")
                return "", "queue_full"
            proposal_id = new_id()
            self.store.execute(
                "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    kind,
                    cluster_key,
                    candidate_id,
                    baseline_id,
                    "pending_review",
                    evidence_count,
                    dumps(report),
                    priority,
                    None,
                    None,
                    at,
                    at,
                    None,
                ),
            )
            return proposal_id, "created"

    def _transition(
        self, proposal_id: str, allowed: tuple[str, ...], status: str, **fields: Any
    ) -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if not proposal:
            raise GovernanceError("Unknown proposal")
        if proposal["status"] not in allowed:
            raise GovernanceError(f"Cannot move a {proposal['status']} proposal to {status}")
        assignments = ", ".join(["status = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields])
        self.store.execute(
            f"UPDATE proposals SET {assignments} WHERE id = ?",
            (status, now(), *fields.values(), proposal_id),
        )
        return proposal

    def _baseline_current(self, proposal: dict[str, Any]) -> bool:
        """Require the complete artifact combination against which the candidate was evaluated."""
        versions = proposal["report"].get("effect", {}).get("baseline_versions", {})
        # Old reports may lack both artifact baselines or may include holdout failure evidence.
        return versions.keys() == set(KINDS) and all(
            self.reg.production(kind).id == version for kind, version in versions.items()
        )

    def approve(
        self,
        proposal_id: str,
        reviewer: str,
        note: str | None = None,
        *,
        expected_candidate_id: str | None = None,
    ) -> None:
        """Start a canary after human approval, never promote directly."""
        with self.store.transaction(immediate=True):
            proposal = self.get(proposal_id)
            if not proposal or proposal["status"] != "pending_review":
                raise GovernanceError("Only a pending proposal can be approved")
            if (
                expected_candidate_id is not None
                and proposal["candidate_id"] != expected_candidate_id
            ):
                raise GovernanceError("Proposal changed; reopen it before approving")
            stale = not self._baseline_current(proposal)
            if stale:
                self._transition(proposal_id, ("pending_review",), "stale")
            else:
                if self.list("canary", limit=1):
                    raise GovernanceError("Finish the current canary before starting another")
                self._transition(
                    proposal_id,
                    ("pending_review",),
                    "canary",
                    reviewer=reviewer,
                    review_note=note,
                    canary_started_at=now(),
                )
                self.reg.set_label(proposal["candidate_id"], "staging")
        if stale:
            raise GovernanceError("Evaluation baseline must be refreshed; proposal marked stale")

    def reject(self, proposal_id: str, reviewer: str, note: str | None = None) -> None:
        """Reject a pending or expired proposal and archive its candidate; reject other states."""
        with self.store.transaction(immediate=True):
            proposal = self._transition(
                proposal_id,
                ("pending_review", "stale"),
                "rejected",
                reviewer=reviewer,
                review_note=note,
            )
            self.reg.set_label(proposal["candidate_id"], "rejected")

    def promote(self, proposal_id: str, reviewer: str) -> None:
        """Require a second review and recompute canary gates from operations data, not caller
        summaries.
        """
        with self.store.transaction(immediate=True):
            proposal = self.get(proposal_id)
            if not proposal or proposal["status"] != "canary":
                raise GovernanceError("Only a proposal in canary can be promoted")
            elapsed_days = (now() - (proposal["canary_started_at"] or now())) / 86_400
            if elapsed_days < settings.CANARY_MIN_DAYS:
                raise GovernanceError(
                    f"Canary has run {elapsed_days:.1f} of {settings.CANARY_MIN_DAYS} days"
                )
            if not self._baseline_current(proposal):
                raise GovernanceError(
                    "Production changed during canary; evaluate again against the current baseline"
                )
            canary_metrics = metrics_for_proposal(self.store, proposal)
            if not enough_samples(canary_metrics):
                raise GovernanceError(
                    "Canary requires at least 20 candidate and 20 control observations"
                )
            if regressed_metrics(canary_metrics, settings.CANARY_MAX_DROP):
                raise GovernanceError("Canary performance regressed; promotion is blocked")
            candidate = self.reg.get(proposal["candidate_id"])
            if candidate is None:
                raise GovernanceError("The candidate artifact no longer exists")
            # Retain lineage so rollback restores the exact predecessor.
            self.store.execute(
                "UPDATE artifact_versions SET parent_id = ? WHERE id = ?",
                (proposal["baseline_id"], candidate.id),
            )
            self.reg.set_label(candidate.id, "production")
            report = {**proposal["report"], "canary_metrics": canary_metrics}
            self._transition(
                proposal_id, ("canary",), "promoted", reviewer=reviewer, report=dumps(report)
            )

    def rollback(
        self, proposal_id: str, reason: str, reviewer: str = "system", *, canary_only: bool = False
    ) -> bool:
        """Stop a canary or restore a promoted version's exact predecessor."""
        with self.store.transaction(immediate=True):
            proposal = self.get(proposal_id)
            if not proposal:
                raise GovernanceError("Unknown proposal")
            if canary_only and proposal["status"] != "canary":
                return False
            if proposal["status"] == "canary":
                self.reg.set_label(proposal["candidate_id"], "rejected")
            elif proposal["status"] == "promoted":
                current = self.reg.production(proposal["kind"])
                if current.id != proposal["candidate_id"]:
                    raise GovernanceError("Only the current production version can be rolled back")
                baseline = self.reg.get(proposal["baseline_id"])
                if not baseline or baseline.kind != proposal["kind"]:
                    raise GovernanceError("The rollback target no longer exists")
                self.reg.set_label(baseline.id, "production")
                self.reg.set_label(current.id, "rejected")
            else:
                raise GovernanceError("Only canary or promoted proposals can be rolled back")
            self._transition(
                proposal_id,
                ("canary", "promoted"),
                "rolled_back",
                reviewer=reviewer,
                review_note=reason,
            )
            return True

    def housekeeping(self) -> dict[str, int]:
        """Expire old proposals and mark those with changed baselines stale."""
        with self.store.transaction(immediate=True):
            at = now()
            expired = 0
            for proposal in self.list("pending_review") + self.list("stale"):
                if at - proposal["created_at"] > settings.PROPOSAL_TTL_DAYS * 86_400:
                    self._transition(proposal["id"], ("pending_review", "stale"), "expired")
                    self.reg.set_label(proposal["candidate_id"], "rejected")
                    expired += 1
            stale = 0
            for proposal in self.list("pending_review"):
                if not self._baseline_current(proposal):
                    self._transition(proposal["id"], ("pending_review",), "stale")
                    stale += 1
            return {"expired": expired, "stale": stale}
