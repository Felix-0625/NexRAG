"""Internal evaluation controls for the same validated production agent path.

Trusted evaluators construct these options; public chat schemas reject them. Pin guidance
and parameters, collect observations in memory, and never write synthetic turns to user memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvaluationRun:
    """Trusted internal overrides for isolated artifact evaluation."""

    guidance: str
    params: dict[str, Any]
    required_tools: list[str] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)
    denied: bool = False
    status: str = "ok"
    authorized_tenants: tuple[str, ...] = ()
    business_adapter: Any | None = None
    business_schema: str | None = None
    knowledge_documents: list[dict[str, Any]] | None = None
    business_metrics: dict[str, Any] | None = None
