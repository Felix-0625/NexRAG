"""First-write indexes must be ready before reporting ingestion success."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import (
    KeywordIndexParams,
    KeywordIndexType,
    Modifier,
    PayloadSchemaType,
    SparseVectorParams,
    TokenizerType,
)

from app.config import Settings
from app.pipelines import indexing
from app.services import qdrant_setup


def test_missing_startup_collection_is_retried_after_successful_first_write(monkeypatch):
    config = Settings(qdrant_index="fresh-instance")
    database = Mock()
    database.get_collection.side_effect = [
        UnexpectedResponse(404, "not found", b"missing", {}),
        SimpleNamespace(
            payload_schema={}, config=SimpleNamespace(params=SimpleNamespace(sparse_vectors={}))
        ),
    ]
    monkeypatch.setattr(qdrant_setup, "_raw_qdrant_client", lambda: database)
    assert qdrant_setup.ensure_payload_indexes(active_settings=config) is False
    database.create_payload_index.assert_not_called()
    pipeline = Mock()
    pipeline.run.return_value = {"writer": {"documents_written": 2}}
    monkeypatch.setattr(indexing, "_pipeline", pipeline)
    monkeypatch.setattr(indexing, "_document_store", Mock())
    monkeypatch.setattr(indexing, "_settings", config)
    monkeypatch.setattr(indexing, "_delete_stale_versions", Mock())
    result = indexing.run_indexing_pipeline(
        "public-original.txt", {"file_id": "f", "collection_name": "authorized"}
    )
    assert result.chunks_count == 2
    assert database.create_payload_index.call_count == 5
    tenant_call = database.create_payload_index.call_args_list[0].kwargs
    assert tenant_call["collection_name"] == "fresh-instance"
    assert tenant_call["field_name"] == "meta.collection_name"
    assert tenant_call["field_schema"].is_tenant is True
    assert tenant_call["wait"] is True
    assert tenant_call["timeout"] > 5


def test_index_failure_rolls_back_only_new_ingest_and_does_not_sweep_old_version(monkeypatch):
    pipeline = Mock()
    pipeline.run.return_value = {"writer": {"documents_written": 2}}
    monkeypatch.setattr(indexing, "_pipeline", pipeline)
    monkeypatch.setattr(indexing, "_document_store", Mock())
    monkeypatch.setattr(indexing, "_settings", Settings())
    monkeypatch.setattr(
        qdrant_setup, "ensure_payload_indexes", Mock(side_effect=RuntimeError("index failed"))
    )
    rollback, sweep = Mock(), Mock()
    monkeypatch.setattr(indexing, "_delete_ingest_version", rollback)
    monkeypatch.setattr(indexing, "_delete_stale_versions", sweep)
    with pytest.raises(RuntimeError, match="index failed"):
        indexing.run_indexing_pipeline("source.txt", {"file_id": "f", "collection_name": "a"})
    rollback.assert_called_once()
    sweep.assert_not_called()


def test_five_channel_index_upgrade_uses_existing_payload_fields(monkeypatch):
    database = Mock()
    database.get_collection.return_value = SimpleNamespace(payload_schema={})
    monkeypatch.setattr(qdrant_setup, "_raw_qdrant_client", lambda: database)

    assert qdrant_setup.ensure_payload_indexes(
        active_settings=Settings(enable_five_channel_retrieval=True)
    )
    requests = {
        call.kwargs["field_name"]: call.kwargs["field_schema"]
        for call in database.create_payload_index.call_args_list
    }
    assert requests["content"].tokenizer is TokenizerType.MULTILINGUAL
    assert requests["meta.filename"].tokenizer is TokenizerType.MULTILINGUAL
    assert requests["meta.headers_breadcrumb"].tokenizer is TokenizerType.MULTILINGUAL
    assert requests["meta.parent_split_id"] is PayloadSchemaType.INTEGER


def test_bge_m3_rejects_an_existing_bm42_collection_without_mutating_it(monkeypatch):
    database = Mock()
    database.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                sparse_vectors={"text-sparse": SparseVectorParams(modifier=Modifier.IDF)}
            )
        )
    )
    monkeypatch.setattr(qdrant_setup, "_raw_qdrant_client", lambda: database)
    with pytest.raises(RuntimeError, match="rebuilt sparse index"):
        qdrant_setup.ensure_payload_indexes(
            active_settings=Settings(
                enable_sparse_embeddings=True, sparse_embedding_model="BAAI/bge-m3"
            )
        )
    database.update_collection.assert_not_called()
    database.create_payload_index.assert_not_called()


def test_existing_correct_indexes_are_not_recreated_and_server_errors_are_not_hidden(monkeypatch):
    database = Mock()
    schema = {
        field: SimpleNamespace(data_type=PayloadSchemaType.KEYWORD, params=None)
        for field in (
            "meta.collection_type",
            "meta.languages",
            "meta.file_id",
            "meta.ingest_version",
        )
    }
    schema["meta.collection_name"] = SimpleNamespace(
        data_type=PayloadSchemaType.KEYWORD,
        params=KeywordIndexParams(type=KeywordIndexType.KEYWORD, is_tenant=True),
    )
    database.get_collection.return_value = SimpleNamespace(
        payload_schema=schema, config=SimpleNamespace(params=SimpleNamespace(sparse_vectors={}))
    )
    monkeypatch.setattr(qdrant_setup, "_raw_qdrant_client", lambda: database)
    assert qdrant_setup.ensure_payload_indexes(active_settings=Settings()) is True
    database.create_payload_index.assert_not_called()
    database.get_collection.side_effect = UnexpectedResponse(
        503, "unavailable", b"unavailable", {}
    )
    with pytest.raises(UnexpectedResponse):
        qdrant_setup.ensure_payload_indexes()
