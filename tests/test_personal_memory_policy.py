"""User opt-out constrains core helpers, not only upstream UI preferences."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.agent.authorization import memory_helpers_allowed
from src.main import app
from src.memory.models import MemoryCandidate, MemoryScope
from src.memory.service import MemoryService
from src.memory.user_policy import memory_enabled, set_memory_enabled
from src.memory.vectors import InMemoryVectorIndex
from src.midplatform.ops_store import OpsStore
from src.midplatform.tenant_access import Principal, get_principal


def candidate():
    """Build a memory candidate with explicit test defaults."""
    return MemoryCandidate(
        "global", "preference", "user", "language", "Chinese", 0.9, "user_explicit", 6
    )


def test_personal_settings_are_durable_and_per_user(tmp_path):
    path = tmp_path / "ops.db"
    first = OpsStore(path)
    set_memory_enabled("alice", False, store=first)
    first.close()
    second = OpsStore(path)
    try:
        assert memory_enabled("alice", store=second) is False
        assert memory_enabled("bob", store=second) is True
    finally:
        second.close()


def test_settings_route_only_changes_signed_in_person_and_rejects_impersonation():
    principal = Principal("alice", "alice@local", "user")
    app.dependency_overrides[get_principal] = lambda: principal
    try:
        with TestClient(app) as client:
            assert (
                client.post("/v1/memory/settings", json={"enabled": False}).json()["enabled"]
                is False
            )
            assert memory_enabled("alice") is False
            assert (
                client.post(
                    "/v1/memory/settings", json={"enabled": True, "user_id": "bob"}
                ).status_code
                == 422
            )
            assert client.post("/v1/memory/settings", json={"enabled": "false"}).status_code == 422
            principal = Principal("admin", "admin@local", "admin")
            assert client.get("/v1/memory/settings").json()["enabled"] is True
            client.post("/v1/memory/settings", json={"enabled": False})
            assert memory_enabled("admin") is False
            assert memory_enabled("bob") is True
    finally:
        app.dependency_overrides.pop(get_principal, None)


def test_memory_helper_checks_personal_setting(monkeypatch):
    monkeypatch.setattr("src.memory.local_policy.local_memory_configuration", lambda: True)
    context = SimpleNamespace(user_id="alice")
    assert memory_helpers_allowed(context)
    set_memory_enabled("alice", False)
    assert not memory_helpers_allowed(context)
    assert memory_helpers_allowed(SimpleNamespace(user_id="bob"))


def test_disabled_memory_does_not_extract_embed_or_recall_but_preserves_records(
    isolated_ops, embedder
):
    service = MemoryService(isolated_ops, InMemoryVectorIndex(), embedder)
    scope = MemoryScope("alice", "sales")
    asyncio.run(service.write(candidate(), scope))
    service.completer = AsyncMock()
    service.embedder.embed = AsyncMock()
    set_memory_enabled("alice", False)
    assert asyncio.run(service.ingest_turn("language preference", scope)) == []
    assert asyncio.run(service.recall("language", scope)) == []
    assert asyncio.run(service.write(candidate(), scope)) is None
    service.completer.assert_not_called()
    service.embedder.embed.assert_not_called()
    assert len(service.store.list_for_user("alice")) == 1
    assert service.forget("alice") == 1


@pytest.mark.parametrize("operation", ["write", "recall"])
def test_opt_out_during_embedding_prevents_late_write_or_delivery(
    isolated_ops, embedder, operation
):
    service = MemoryService(isolated_ops, InMemoryVectorIndex(), embedder)
    scope = MemoryScope("alice", "sales")
    asyncio.run(service.write(candidate(), scope))
    original = embedder.embed

    async def disable_while_embedding(texts):
        """Provide disable while embedding test data or behavior for this scenario."""
        result = await original(texts)
        set_memory_enabled("alice", False)
        return result

    embedder.embed = disable_while_embedding
    if operation == "write":
        assert asyncio.run(service.write(candidate(), scope)) is None
    else:
        assert asyncio.run(service.recall("language", scope)) == []
    assert len(service.store.list_for_user("alice")) == 1


def test_opt_out_during_extraction_prevents_new_records(isolated_ops, embedder):
    async def completer(system, user):
        """Provide completer test data or behavior for this scenario."""
        set_memory_enabled("alice", False)
        return '{"items":[{"scope":"global","kind":"preference","subject":"user","attribute":"language","value":"Chinese","confidence":0.9,"importance":6,"explicit":true}]}'

    service = MemoryService(isolated_ops, InMemoryVectorIndex(), embedder, completer=completer)
    assert asyncio.run(service.ingest_turn("我偏好中文回答", MemoryScope("alice", "sales"))) == []
    assert service.store.list_for_user("alice") == []


def test_automation_finish_records_trace_without_extracting_task_instruction(monkeypatch):
    from unittest.mock import Mock

    from src.agent.dependencies import AgentDeps, UserContext
    from src.agent.execution import AgentExecution
    from src.observability.tracer import TraceRecord

    record = Mock()
    service = Mock(side_effect=AssertionError("Automation must not start memory extraction"))
    monkeypatch.setattr("src.agent.execution.tracer.record", record)
    monkeypatch.setattr("src.memory.runtime.get_memory_service", service)
    context = UserContext(user_id="alice", email="alice@local", tenant_id="", capture_memory=False)
    trace = TraceRecord(
        trace_id="synthetic-task",
        user_id="alice",
        tenant_id="",
        chat_id="task-chat",
        input_text=None,
    )
    AgentExecution()._finish(
        trace,
        AgentDeps(context=context),
        prompt="Reply TASK_MARKER",
        output="TASK_MARKER",
        messages=[],
        status="ok",
    )
    record.assert_called_once()
    service.assert_not_called()


def test_webui_automation_bridge_sets_deny_only_memory_capture_header():
    import ast
    from pathlib import Path

    source = ast.parse(Path("webui/backend/open_webui/routers/openai.py").read_text("utf8"))
    assignment = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "X-NexRAG-Capture-Memory"
            for target in node.targets
        )
    )
    for metadata, expected in [
        ({"automation_id": "scheduled-fixture"}, "false"),
        ({}, "true"),
        (None, "true"),
    ]:
        namespace = {"headers": {}, "metadata": metadata}
        exec(
            compile(
                ast.Module(body=[assignment], type_ignores=[]),
                "<actual-webui-memory-capture>",
                "exec",
            ),
            namespace,
        )
        assert namespace["headers"]["X-NexRAG-Capture-Memory"] == expected
