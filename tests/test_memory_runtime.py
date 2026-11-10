"""Memory helpers require local schema-constrained extraction."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.config import settings
from src.memory import runtime
from src.memory.extractor import Extraction


def test_extraction_requests_numeric_schema(monkeypatch):
    monkeypatch.setattr(runtime, "local_memory_configuration", lambda: True)
    monkeypatch.setattr(settings, "LLM_MODEL_NAME", "synthetic-local-model")
    captured = {}
    transport = httpx.MockTransport(lambda request: capture_completion(request, captured))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        runtime.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    result = asyncio.run(runtime.memory_complete("Extract preferences", "I prefer tables"))
    assert Extraction.model_validate_json(result).items == []
    response_format = captured["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]["schema"]
    item = schema["$defs"]["ExtractedItem"]["properties"]
    assert item["confidence"] == {
        "minimum": 0,
        "maximum": 1,
        "title": "Confidence",
        "type": "number",
    }
    assert item["importance"]["type"] == "number"
    assert item["importance"]["minimum"] == 1
    assert item["importance"]["maximum"] == 10
    assert captured["temperature"] == 0


def capture_completion(request, captured):
    """Provide capture completion test data or behavior for this scenario."""
    captured.update(json.loads(request.content))
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"items": []}'}}]})


@pytest.mark.parametrize("configuration,model", [(False, "local"), (True, "model:cloud")])
def test_extraction_rejects_cloud_before_network(monkeypatch, configuration, model):
    monkeypatch.setattr(runtime, "local_memory_configuration", lambda: configuration)
    monkeypatch.setattr(settings, "LLM_MODEL_NAME", model)

    def forbidden_client(**kwargs):
        """Provide forbidden client test data or behavior for this scenario."""
        pytest.fail(
            "Cloud/disallowed extraction must be rejected before constructing an HTTP client"
        )

    monkeypatch.setattr(runtime.httpx, "AsyncClient", forbidden_client)
    with pytest.raises(ValueError, match="require local models"):
        asyncio.run(runtime.memory_complete("extract", "synthetic preferences"))
