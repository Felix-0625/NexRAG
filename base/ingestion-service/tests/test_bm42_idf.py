"""BM42 requires Qdrant IDF on both new and previously created collections."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from haystack import Document
from haystack.dataclasses import SparseEmbedding
from haystack_integrations.document_stores.qdrant import QdrantDocumentStore
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.config import Settings
from app.pipelines import indexing
from app.services import qdrant_setup


def settings(**overrides):
    return Settings(
        api_key="test-api-key-long-enough-for-validation", embedding_dim=4, **overrides
    )


@pytest.mark.parametrize(
    "model,modifier",
    [
        ("Qdrant/bm42-all-minilm-l6-v2-attentions", Modifier.IDF),
        ("prithivida/Splade_PP_en_v1", None),
    ],
)
def test_factory_creates_correct_sparse_index(monkeypatch, model, modifier):
    def local_store(**kwargs):
        kwargs.pop("url")
        return QdrantDocumentStore(location=":memory:", **kwargs)

    monkeypatch.setattr(indexing, "QdrantDocumentStore", local_store)
    store = indexing._build_document_store(
        settings(sparse_embedding_model=model, enable_sparse_embeddings=True)
    )
    store.write_documents(
        [
            Document(
                content="policy",
                embedding=[0.1] * 4,
                sparse_embedding=SparseEmbedding(indices=[1], values=[1.0]),
            )
        ]
    )
    # This is an actual written Qdrant collection, not a configuration assertion.
    info = store._client.get_collection(store.index)
    assert info.config.params.sparse_vectors["text-sparse"].modifier == modifier
    assert info.points_count == 1
    store._client.close()


def test_existing_bm42_idf_upgrade_preserves_collection(monkeypatch):
    client = Mock()
    client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(sparse_vectors={"text-sparse": SparseVectorParams()})
        )
    )
    monkeypatch.setattr(qdrant_setup, "_raw_qdrant_client", lambda: client)
    monkeypatch.setattr(qdrant_setup, "settings", settings(enable_sparse_embeddings=True))
    qdrant_setup.ensure_payload_indexes()
    assert (
        client.update_collection.call_args.kwargs["sparse_vectors_config"]["text-sparse"].modifier
        == Modifier.IDF
    )
    client.delete_collection.assert_not_called()
    client.delete.assert_not_called()


def test_failed_modifier_upgrade_is_not_ready(monkeypatch):
    client = Mock()
    client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(sparse_vectors={"text-sparse": SparseVectorParams()})
        )
    )
    client.update_collection.side_effect = RuntimeError("cannot update index")
    monkeypatch.setattr(qdrant_setup, "_raw_qdrant_client", lambda: client)
    monkeypatch.setattr(qdrant_setup, "settings", settings(enable_sparse_embeddings=True))
    with pytest.raises(RuntimeError, match="cannot update index"):
        qdrant_setup.ensure_payload_indexes()


def test_real_existing_bm42_index_upgrade_keeps_vectors_and_payload(monkeypatch):
    """Exercise the actual Qdrant update semantics, beyond mocking a call."""
    client = QdrantClient(location=":memory:")
    config = settings(enable_sparse_embeddings=True)
    try:
        client.create_collection(
            config.qdrant_index,
            vectors_config={"text-dense": VectorParams(size=4, distance=Distance.COSINE)},
            sparse_vectors_config={"text-sparse": SparseVectorParams()},
        )
        client.upsert(
            config.qdrant_index,
            [
                PointStruct(
                    id=1,
                    vector={
                        "text-dense": [0.5] * 4,
                        "text-sparse": SparseVector(indices=[1], values=[0.4]),
                    },
                    payload={"content": "child", "meta": {"parent_text": "parent"}},
                )
            ],
        )
        before = client.retrieve(config.qdrant_index, [1], with_vectors=True)[0]
        monkeypatch.setattr(qdrant_setup, "_raw_qdrant_client", lambda: client)
        monkeypatch.setattr(qdrant_setup, "settings", config)
        qdrant_setup.ensure_payload_indexes()
        qdrant_setup.ensure_payload_indexes()  # Restart upgrades must be idempotent.
        after = client.retrieve(config.qdrant_index, [1], with_vectors=True)[0]
        info = client.get_collection(config.qdrant_index)
        assert info.config.params.sparse_vectors["text-sparse"].modifier == Modifier.IDF
        assert info.points_count == 1
        assert before.vector == after.vector
        assert before.payload == after.payload
    finally:
        client.close()
