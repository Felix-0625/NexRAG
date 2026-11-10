"""Resolve production or canary artifacts for one request.

When staging exists, hash user IDs into stable cohorts so users keep the same versions
throughout the observation window; everyone else uses production.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from src.config import settings
from src.evolution.artifacts import ArtifactRegistry, registry


@dataclass
class ActiveArtifacts:
    """Prompt and parameter versions selected for one user request."""

    prompt: str
    params: dict[str, Any]
    versions: dict[str, str] = field(default_factory=dict)
    canary: bool = False


def in_canary(user_id: str, ratio: float | None = None) -> bool:
    """Choose stable canary membership from the user hash and configured or explicit ratio."""
    ratio = settings.CANARY_RATIO if ratio is None else ratio
    bucket = int(hashlib.sha256(f"canary:{user_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < ratio


def resolve(user_id: str, reg: ArtifactRegistry | None = None) -> ActiveArtifacts:
    """Resolve prompt and parameter versions, preferring staging only for eligible canary users."""
    reg = reg or registry
    canary_user = in_canary(user_id)
    chosen: dict[str, Any] = {}
    versions: dict[str, str] = {}
    used_canary = False
    for kind in ("prompt", "params"):
        artifact = reg.production(kind)
        staging = reg.by_label(kind, "staging")
        if canary_user and staging is not None:
            artifact = staging
            used_canary = True
        chosen[kind] = artifact.content
        versions[kind] = artifact.id
    return ActiveArtifacts(chosen["prompt"], dict(chosen["params"]), versions, used_canary)
