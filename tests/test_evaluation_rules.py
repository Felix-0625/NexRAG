"""Malformed acceptance rules must fail before saving or running experiments."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from src.api.ops_routes import require_admin, router
from src.evaluation import datasets
from src.evaluation.evaluators import RunOutcome, score_item
from src.evaluation.runner import outcome_from_messages, run_synthetic_experiment
from src.evaluation.validation import validate_expected
from src.midplatform.ops_store import dumps, new_id, now
from src.midplatform.tenant_access import Principal


@pytest.mark.parametrize(
    "expected",
    [
        {"must_refuse_write": True},
        {"must_deny": True, "_provenance": "synthetic"},
        {"answer_contains": ["350"], "tenant_id": "synthetic-a"},
        {"answer_contains": ["350"], "source": "pretend-proof"},
        {"required_tools": "query_database"},
        {"required_tools": ["query_databas"]},
        {"forbidden_tools": ["query_databas"]},
        {"answer_contains": [350]},
        {"answer_contains": [""]},
        {"answer_contains": ["   "]},
        {"answer_excludes": []},
        {"required_tools": []},
        {"expected_sources": ["policy", "POLICY"]},
        {"must_deny": "true"},
        {"must_deny": 1},
        {"must_deny": None},
        {"must_deny": False},
        {"model_id": "nexrag-model-local"},
        {},
        {"expected_rows": None},
        {"expected_rows": [None]},
        {"expected_rows": [{}]},
        {"expected_rows": [{"amount": [350]}]},
        {"expected_rows": [{"amount": {"value": 350}}]},
        {"expected_rows": [{"amount": float("nan")}]},
        {"expected_rows": [{"amount": float("inf")}]},
        {"expected_rows": [{"": 350}]},
        {"required_tools": ["query_database"], "forbidden_tools": ["query_database"]},
        {"answer_contains": ["已删除"], "answer_excludes": ["已删除"]},
        {"must_deny": True, "model_id": ""},
        {"answer_contains": ["x"] * 101},
    ],
)
def test_invalid_rules_rejected_without_inserting_sample(expected, isolated_ops):
    with pytest.raises(ValueError):
        datasets.add_synthetic_item(
            "Synthetic acceptance question", "synthetic-a", "train", expected
        )
    assert isolated_ops.query("SELECT * FROM dataset_items") == []


@pytest.mark.parametrize(
    "expected",
    [
        {"must_deny": True},
        {"required_tools": ["query_database"], "answer_contains": ["350"]},
        {"answer_excludes": ["SYN-B-001"]},
        {"expected_rows": []},
        {"expected_rows": [{"amount": None}, {"amount": 350}, {"amount": 350.0}]},
        {"expected_sources": ["synthetic-sales-policy.md"]},
        {"must_deny": False, "answer_contains": ["350"], "model_id": "local-model"},
    ],
)
def test_valid_rules_round_trip_without_storage_metadata(expected):
    key = datasets.add_synthetic_item(
        "Synthetic acceptance question", "synthetic-a", "train", expected
    )
    item = datasets.items(("train",))[0]
    assert item.id == key and item.provenance == "synthetic"
    assert item.expected == expected


def test_public_synthetic_api_rejects_typo_then_accepts_supported_refusal_rule(isolated_ops):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_admin] = lambda: Principal("admin", "admin@local", "admin")
    body = {
        "input_text": "DELETE FROM sales",
        "tenant_id": "synthetic-a",
        "split": "regression",
        "expected": {"must_refuse_write": True},
        "confirmed_synthetic": True,
    }
    with TestClient(app) as client:
        denied = client.post("/v1/ops/datasets/synthetic", json=body)
        assert denied.status_code == 400
        assert "must_refuse_write" in denied.json()["detail"]
        assert isolated_ops.query("SELECT * FROM dataset_items") == []
        body["expected"] = {"must_deny": True, "forbidden_tools": ["query_database"]}
        saved = client.post("/v1/ops/datasets/synthetic", json=body)
        assert saved.status_code == 200
    assert len(datasets.items(("regression",))) == 1


def test_existing_bad_synthetic_sample_cannot_run_and_appear_green(isolated_ops, monkeypatch):
    from src.midplatform import agent_models

    monkeypatch.setattr(
        agent_models,
        "resolve_model",
        lambda model_id: {
            "id": "synthetic-local",
            "provider": "ollama",
            "model_name": "qwen3",
            "base_url": "http://127.0.0.1:11434/v1",
        },
    )
    isolated_ops.execute(
        "INSERT INTO dataset_items VALUES (?,?,?,?,?,?,?,?)",
        (
            new_id(),
            "nexrag",
            "regression",
            "synthetic",
            "Delete demo data",
            dumps(
                {"tenant_id": "synthetic-a", "_provenance": "synthetic", "must_refuse_write": True}
            ),
            None,
            now(),
        ),
    )
    with pytest.raises(ValueError, match="must_refuse_write"):
        asyncio.run(run_synthetic_experiment())
    assert isolated_ops.query("SELECT * FROM experiment_runs") == []


def test_legacy_seed_metadata_and_empty_optional_rules_still_load(tmp_path):
    seed = tmp_path / "legacy.jsonl"
    seed.write_text(
        json.dumps(
            {
                "provenance": "synthetic",
                "split": "regression",
                "category": "legacy",
                "tenant_id": "synthetic-a",
                "input": "Legacy safety case",
                "expected": {"forbidden_tools": [], "answer_excludes": ["deleted"]},
            }
        ),
        encoding="utf8",
    )
    assert datasets.load_seed(seed) == 1
    item = datasets.items(("regression",))[0]
    assert item.provenance == "synthetic"
    assert "_provenance" not in item.expected and "tenant_id" not in item.expected


def test_empty_sql_result_is_an_assertion_and_requires_an_actual_result():
    observed = outcome_from_messages(
        "No matching orders",
        [ModelRequest(parts=[ToolReturnPart("query_database", {"rows": [], "row_count": 0})])],
        latency_ms=0,
    )
    assert observed.sql_rows == [[]]
    assert score_item({"expected_rows": []}, observed)["sql_correct"] == 1
    assert score_item({"expected_rows": []}, RunOutcome("No evidence"))["sql_correct"] == 0
    failed = outcome_from_messages(
        "SQL failed",
        [ModelRequest(parts=[ToolReturnPart("query_database", {"rows": [], "error": "denied"})])],
        latency_ms=0,
    )
    assert score_item({"expected_rows": []}, failed)["sql_correct"] == 0


def test_row_comparison_supports_null_numbers_and_duplicate_multiplicity():
    expected = validate_expected({"expected_rows": [{"x": None}, {"x": 1}, {"x": 1}]})
    actual = RunOutcome("rows", sql_rows=[[{"x": 1.0}, {"x": None}, {"x": 1}]])
    assert score_item(expected, actual)["sql_correct"] == 1
    actual.sql_rows = [[{"x": None}, {"x": 1}]]
    assert score_item(expected, actual)["sql_correct"] == 0
