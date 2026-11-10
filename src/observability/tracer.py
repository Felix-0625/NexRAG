"""Persist traces, observations, and rule scores locally.

Use a Langfuse-style data model for offline evaluation and evolution. Reconstruct
observations from completed PydanticAI messages without an OpenTelemetry exporter.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from src.config import settings
from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now
from src.observability.redaction import hash_user, store_content

log = logging.getLogger(__name__)

TOOL_TYPES = {
    "query_database": "tool",
    "search_knowledge_documents": "retrieval",
    "search_web_engine": "retrieval",
    "inspect_local_file": "tool",
    "plan_analysis": "tool",
    "get_business_schema": "tool",
    "recall_memory": "memory",
}


@dataclass
class TraceRecord:
    """Facts collected from one run before persistence."""

    trace_id: str
    user_id: str
    tenant_id: str | None
    chat_id: str | None
    input_text: str
    artifact_versions: dict[str, str] = field(default_factory=dict)
    canary: bool = False
    started_at: float = field(default_factory=now)


def _summarize(value: Any, limit: int = 600) -> str:
    """Return compact redacted JSON-like tool summaries."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return store_content(text, limit) or ""


def _tool_error(content: Any) -> str | None:
    """Detect tool-level failures inside otherwise successful returns."""
    if isinstance(content, dict):
        if content.get("error"):
            return str(content["error"])[:300]
        if content.get("reflection_hint"):
            return "reflection: " + str(content["reflection_hint"])[:200]
    return None


def extract_observations(messages: Iterable[ModelMessage]) -> list[dict[str, Any]]:
    """Pair calls with returns or retries into ordered observations."""
    calls: dict[str, ToolCallPart] = {}
    observations: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    calls[part.tool_call_id] = part
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_name:
                    call = calls.get(part.tool_call_id)
                    # PydanticAI encodes ModelRetry with a typed ToolReturnPart outcome;
                    # never infer failure from arbitrary document or tool-text prefixes.

                    outcome = part.outcome if isinstance(part, ToolReturnPart) else "retried"
                    content = part.content
                    observations.append(
                        {
                            "type": TOOL_TYPES.get(part.tool_name, "tool"),
                            "name": part.tool_name,
                            "input_summary": _summarize(call.args if call else None),
                            "output_summary": _summarize(content),
                            "error": (
                                "retry: " + _summarize(content, 300)
                                if outcome == "retried"
                                else (
                                    f"tool outcome: {outcome}"
                                    if outcome in {"failed", "denied", "interrupted"}
                                    else _tool_error(content)
                                )
                            ),
                        }
                    )
    return observations


def _insert_trace(conn, trace, private, output_text, latency_ms, status, error) -> None:
    """Insert a trace without raw private input, output, or errors."""
    conn.execute(
        "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trace.trace_id,
            hash_user(trace.user_id),
            trace.tenant_id,
            trace.chat_id,
            dumps(trace.artifact_versions),
            int(trace.canary),
            None if private else store_content(trace.input_text),
            None if private else store_content(output_text),
            latency_ms,
            status,
            ("execution_error" if error else None) if private else ((error or "")[:500] or None),
            trace.started_at,
        ),
    )


def _insert_observations(conn, trace_id, observations, private) -> None:
    """Insert ordered observations, keeping only failure flags for private traces."""
    for position, obs in enumerate(observations):
        conn.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                trace_id,
                obs["type"],
                obs["name"],
                None if private else obs["input_summary"],
                None if private else obs["output_summary"],
                ("tool_execution_error" if obs["error"] else None) if private else obs["error"],
                position,
            ),
        )


def _insert_scores(conn, trace_id, scores) -> None:
    """Insert deterministic rule scores."""
    for name, value in scores.items():
        conn.execute(
            "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), trace_id, name, float(value), "numeric", "rule", None, now()),
        )


class Tracer:
    """Persist traces and scores without failing user requests."""

    def __init__(self, store: OpsStore | None = None):
        self._store = store

    @property
    def store(self) -> OpsStore:
        """Resolve the current operations store lazily."""
        return self._store or get_ops_store()

    def record(
        self,
        trace: TraceRecord,
        *,
        output_text: str | None,
        messages: Iterable[ModelMessage] = (),
        status: str = "ok",
        error: str | None = None,
        rule_scores: dict[str, float] | None = None,
    ) -> str | None:
        """Record a completed run and return its ID, or None when disabled or failed."""
        if not settings.ENABLE_TRACING:
            return None
        try:
            observations = extract_observations(messages)
            private = trace.artifact_versions.get("content_scope") == "private"
            latency_ms = int((now() - trace.started_at) * 1000)
            with self.store.transaction() as conn:
                _insert_trace(conn, trace, private, output_text, latency_ms, status, error)
                _insert_observations(conn, trace.trace_id, observations, private)
                scores = dict(rule_scores or {})
                scores["latency_ms"] = float(latency_ms)
                scores["tool_error_count"] = float(sum(1 for o in observations if o["error"]))
                scores["run_success"] = 1.0 if status == "ok" else 0.0
                _insert_scores(conn, trace.trace_id, scores)
            return trace.trace_id
        except Exception:  # noqa: BLE001 - Observability must never fail a request.
            log.exception("Failed to record trace %s", trace.trace_id)
            return None

    def record_rating(self, trace_id: str, value: float, comment: str | None = None) -> None:
        """Replace prior ratings so retries and changed votes count once."""
        with self.store.transaction() as conn:
            conn.execute(
                "DELETE FROM scores WHERE trace_id = ? AND name = 'user_rating' AND source = 'user'",
                (trace_id,),
            )
            conn.execute(
                "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)",
                (new_id(), trace_id, "user_rating", value, "numeric", "user", comment, now()),
            )

    def purge_expired(self) -> int:
        """Delete expired traces while retaining dataset copies."""
        cutoff = now() - settings.TRACE_RETENTION_DAYS * 86_400
        rows = self.store.query("SELECT COUNT(*) AS n FROM traces WHERE created_at < ?", (cutoff,))
        self.store.execute("DELETE FROM traces WHERE created_at < ?", (cutoff,))
        return int(rows[0]["n"]) if rows else 0


tracer = Tracer()
