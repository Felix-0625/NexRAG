"""Versioned, evolvable prompt and parameter artifacts.

Evolution may modify only these artifacts, never code or the immutable safety prefix.
Following Langfuse conventions, production serves traffic, staging serves canaries,
candidate awaits evaluation or review, and archived retains rollback history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.agent.prompts import DEFAULT_GUIDANCE
from src.config import DEFAULT_KNOWLEDGE_TOP_K, MAX_KNOWLEDGE_TOP_K, settings
from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now

KINDS = ("prompt", "params")
LABELS = ("candidate", "staging", "production", "archived", "rejected")

# Bound the parameters the evolution cycle may tune.
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "retrieval_min_relevance": (0.10, 0.80),
    "max_tool_retries": (0, 4),
    "prune_max_tables": (1, 4),
    "knowledge_top_k": (1, MAX_KNOWLEDGE_TOP_K),
}


def default_params() -> dict[str, float]:
    """Return baseline parameters from deployment settings."""
    return {
        "retrieval_min_relevance": settings.RETRIEVAL_MIN_RELEVANCE,
        "max_tool_retries": settings.MAX_TOOL_RETRIES,
        "prune_max_tables": 2,
        "knowledge_top_k": DEFAULT_KNOWLEDGE_TOP_K,
    }


def clamp_params(params: dict[str, Any]) -> dict[str, float]:
    """Discard unknown keys and clamp values to PARAM_BOUNDS."""
    clean: dict[str, float] = {}
    for key, (low, high) in PARAM_BOUNDS.items():
        if key in params:
            value = float(params[key])
            value = min(max(value, low), high)
            clean[key] = int(value) if isinstance(low, int) and isinstance(high, int) else value
    return clean


@dataclass(frozen=True)
class Artifact:
    """Immutable versioned prompt or parameter artifact."""

    id: str
    kind: str
    content: Any
    label: str
    parent_id: str | None
    note: str | None


class ArtifactRegistry:
    """Persist artifact versions and manage production, canary, and archive labels."""

    def __init__(self, store: OpsStore | None = None):
        self._store = store

    @property
    def store(self) -> OpsStore:
        """Resolve the current operations store lazily."""
        return self._store or get_ops_store()

    @staticmethod
    def _row_to_artifact(row: dict[str, Any]) -> Artifact:
        return Artifact(
            id=row["id"],
            kind=row["kind"],
            content=json.loads(row["content"]),
            label=row["label"],
            parent_id=row["parent_id"],
            note=row["note"],
        )

    def create(
        self,
        kind: str,
        content: Any,
        *,
        label: str = "candidate",
        parent_id: str | None = None,
        note: str | None = None,
        created_by: str = "system",
    ) -> Artifact:
        """Save a new immutable artifact version."""
        if kind not in KINDS or label not in LABELS:
            raise ValueError(f"Unsupported artifact kind/label: {kind}/{label}")
        if kind == "params":
            content = clamp_params(content)
        artifact_id = new_id()
        self.store.execute(
            "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?)",
            (artifact_id, kind, dumps(content), parent_id, label, note, created_by, now()),
        )
        return Artifact(artifact_id, kind, content, label, parent_id, note)

    def get(self, artifact_id: str) -> Artifact | None:
        """Return an artifact by ID, or None if absent."""
        row = self.store.query_one("SELECT * FROM artifact_versions WHERE id = ?", (artifact_id,))
        return self._row_to_artifact(row) if row else None

    def by_label(self, kind: str, label: str) -> Artifact | None:
        """Return the newest version of a kind with this label, or None."""
        row = self.store.query_one(
            "SELECT * FROM artifact_versions WHERE kind = ? AND label = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (kind, label),
        )
        return self._row_to_artifact(row) if row else None

    def production(self, kind: str) -> Artifact:
        """Return production artifacts, installing built-in defaults on first access."""
        current = self.by_label(kind, "production")
        if current:
            if kind == "prompt":
                from src.evolution.builtin_migrations import upgrade_shared_knowledge_default

                upgrade_shared_knowledge_default(self.store, current)
                # Another process may already have completed this migration.
                return self.by_label(kind, "production") or current
            return current
        with self.store.transaction(immediate=True):
            # Concurrent first readers must reload defaults already installed by the winner.
            current = self.by_label(kind, "production")
            if current:
                return current
            content = DEFAULT_GUIDANCE if kind == "prompt" else default_params()
            return self.create(kind, content, label="production", note="built-in default")

    def set_label(self, artifact_id: str, label: str) -> None:
        """Move a version to a label, allowing one production and staging version per kind."""
        artifact = self.get(artifact_id)
        if not artifact or label not in LABELS:
            raise ValueError("Unknown artifact or label")
        with self.store.transaction() as conn:
            if label in {"production", "staging"}:
                conn.execute(
                    "UPDATE artifact_versions SET label = 'archived' "
                    "WHERE kind = ? AND label = ? AND id != ?",
                    (artifact.kind, label, artifact_id),
                )
            conn.execute(
                "UPDATE artifact_versions SET label = ? WHERE id = ?", (label, artifact_id)
            )

    def history(self, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        """List recent version metadata without content, newest first and bounded by limit."""
        return self.store.query(
            "SELECT id, kind, label, parent_id, note, created_by, created_at "
            "FROM artifact_versions WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
            (kind, limit),
        )


registry = ArtifactRegistry()
