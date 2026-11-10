"""Long-term memory scopes, types, provenance, and states."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCOPES = ("global", "tenant", "session")
KINDS = ("fact", "preference", "profile", "task_state", "episode")
# Higher ranks are more trusted; inferred memories must never override explicit user statements.
SOURCE_RANK = {"agent_inferred": 0, "user_implicit": 1, "user_explicit": 2}
# Only these types may cross tenants through global scope.
CROSS_TENANT_KINDS = ("preference", "profile")
SCOPE_WEIGHT = {"session": 1.0, "tenant": 0.85, "global": 0.7}


@dataclass(frozen=True)
class MemoryScope:
    """Server-derived visibility scope that the model cannot supply."""

    user_id: str
    tenant_id: str | None = None
    chat_id: str | None = None


@dataclass
class MemoryCandidate:
    """A fact extracted from user speech before conflict resolution."""

    scope: str
    kind: str
    subject: str
    attribute: str
    value_text: str
    confidence: float
    source: str
    importance: float
    evidence_ref: str | None = None

    def slot(self) -> tuple[str, str]:
        """Return a normalized subject-attribute slot key for conflict matching."""
        return (normalize(self.subject), normalize(self.attribute))


@dataclass
class MemoryRecord:
    """Persisted memory with ownership, history, and lifecycle metadata."""

    id: str
    user_id: str
    tenant_id: str | None
    chat_id: str | None
    scope: str
    kind: str
    subject: str
    attribute: str
    value_text: str
    confidence: float
    source: str
    evidence_ref: str | None
    importance: float
    valid_from: float
    valid_to: float | None
    created_at: float
    status: str
    supersedes_id: str | None = None
    disputed_with: str | None = None
    access_count: int = 0
    last_accessed: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def normalize(text: str) -> str:
    """Normalize slot keys by case, whitespace, and punctuation."""
    cleaned = "".join(ch for ch in text.lower().strip() if ch.isalnum() or ch in " _-")
    return " ".join(cleaned.split())
