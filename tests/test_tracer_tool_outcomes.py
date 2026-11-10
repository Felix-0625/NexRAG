"""Typed tool outcomes survive tracing without revealing private reasons."""

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from src.config import settings
from src.midplatform.ops_store import OpsStore
from src.observability.tracer import Tracer, TraceRecord, extract_observations


def messages_with_outcomes():
    """Provide messages with outcomes test data or behavior for this scenario."""
    parts = [
        ToolReturnPart(
            "calculate_evidence_arithmetic",
            "Select the requested period",
            "retry",
            outcome="retried",
        ),
        ToolReturnPart("read_text_file", "private reason", "failed", outcome="failed"),
        ToolReturnPart("query_database", "private reason", "denied", outcome="denied"),
        ToolReturnPart("search_web_engine", "private reason", "interrupted", outcome="interrupted"),
        ToolReturnPart(
            "read_text_file", "retry: this is ordinary file text about failed requests", "success"
        ),
        ToolReturnPart(
            "read_text_file", {"error": "Explicit application tool error"}, "application-error"
        ),
        RetryPromptPart("Legacy model retry", tool_name="read_text_file", tool_call_id="legacy"),
    ]
    return [
        ModelResponse(
            parts=[
                ToolCallPart(part.tool_name, {"request": "synthetic"}, part.tool_call_id)
                for part in parts
            ]
        ),
        ModelRequest(parts=parts),
    ]


def test_typed_outcomes_and_legacy_retries_count_without_guessing_string_content(monkeypatch):
    monkeypatch.setattr(settings, "TRACE_STORE_CONTENT", "redacted")
    observations = extract_observations(messages_with_outcomes())
    assert observations[0]["error"] == "retry: Select the requested period"
    assert [item["error"] for item in observations[1:4]] == [
        "tool outcome: failed",
        "tool outcome: denied",
        "tool outcome: interrupted",
    ]
    assert observations[4]["error"] is None
    assert observations[5]["error"] == "Explicit application tool error"
    assert observations[6]["error"] == "retry: Legacy model retry"


def test_persisted_counter_includes_retried_and_terminal_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ENABLE_TRACING", True)
    monkeypatch.setattr(settings, "TRACE_STORE_CONTENT", "redacted")
    store = OpsStore(tmp_path / "trace.db")
    trace = TraceRecord("outcomes", "synthetic-user", None, None, "synthetic request")
    assert (
        Tracer(store).record(trace, output_text="done", messages=messages_with_outcomes())
        == "outcomes"
    )
    score = store.query_one(
        "SELECT value FROM scores WHERE trace_id=? AND name='tool_error_count'", ("outcomes",)
    )
    assert score["value"] == 6
    assert (
        store.query_one("SELECT COUNT(*) AS n FROM observations WHERE trace_id=?", ("outcomes",))[
            "n"
        ]
        == 7
    )


def test_private_trace_outcome_reason_and_summaries_remain_suppressed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ENABLE_TRACING", True)
    monkeypatch.setattr(settings, "TRACE_STORE_CONTENT", "full")
    store = OpsStore(tmp_path / "private.db")
    trace = TraceRecord(
        "private-outcomes",
        "synthetic-user",
        None,
        None,
        "private prompt",
        artifact_versions={"content_scope": "private"},
    )
    Tracer(store).record(trace, output_text="private answer", messages=messages_with_outcomes())
    rows = store.query(
        "SELECT input_summary,output_summary,error FROM observations WHERE trace_id=? ORDER BY position",
        (trace.trace_id,),
    )
    assert all(row["input_summary"] is None and row["output_summary"] is None for row in rows)
    assert [row["error"] for row in rows] == ["tool_execution_error"] * 4 + [None] + [
        "tool_execution_error"
    ] * 2
    saved = store.query_one(
        "SELECT input_text,output_text FROM traces WHERE id=?", (trace.trace_id,)
    )
    assert saved == {"input_text": None, "output_text": None}
