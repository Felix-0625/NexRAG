"""Qdrant startup hook.

``QdrantDocumentStore`` creates the collection on first write but doesn't
create payload indexes. We do that explicitly here so per-tenant HNSW kicks
in from day one — important for retrieval performance once the agent is
rewritten in Phase 3.
"""

import logging

from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import (
    FieldCondition,
    Filter,
    KeywordIndexParams,
    KeywordIndexType,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    SparseVectorParams,
    TextIndexParams,
    TextIndexType,
    TokenizerType,
    UpdateStatus,
)

from app.config import Settings, settings
from app.pipelines.indexing import _raw_qdrant_client

log = logging.getLogger(__name__)
_INDEX_BUILD_TIMEOUT_SECONDS = 120


def _index_matches(field_name: str, existing, expected: PayloadSchemaType) -> bool:
    tenant = field_name != "meta.collection_name" or getattr(
        getattr(existing, "params", None), "is_tenant", False
    )
    return bool(existing is not None and existing.data_type == expected and tenant)


def ensure_payload_indexes(
    *, active_settings: Settings | None = None, active_ingest_version: str | None = None
) -> bool:
    """Create the payload indexes: the tenant index on ``meta.collection_name``
    plus plain keyword indexes (collection_type, languages, file_id,
    ingest_version). Five-channel installs also index existing body, filename,
    heading and Parent-ordinal payloads.

    Return False only while the collection does not exist. The ingestion path
    retries immediately after its first successful write, so a fresh instance
    does not need a restart to create its tenant/filter indexes. Existing
    correctly typed indexes are retained without redundant creation requests.
    """
    client = _raw_qdrant_client()
    config = active_settings or settings
    index_name = config.qdrant_index

    try:
        collection = client.get_collection(index_name)
    except UnexpectedResponse as exc:
        if exc.status_code != 404:
            raise
        log.info(
            "qdrant collection %r not yet created; payload indexes will be added after "
            "the first successful ingest (%s)",
            index_name,
            exc,
        )
        return False

    model_name, recorded_model = _ensure_sparse_layout(
        client, collection, config, index_name, active_ingest_version
    )

    indexes = _payload_index_specs(config)
    for field_name, schema, expected in indexes:
        existing = (getattr(collection, "payload_schema", None) or {}).get(field_name)
        if _index_matches(field_name, existing, expected):
            continue
        try:
            result = client.create_payload_index(
                collection_name=index_name,
                field_name=field_name,
                field_schema=schema,
                wait=True,
                timeout=_INDEX_BUILD_TIMEOUT_SECONDS,
            )
            if result.status == UpdateStatus.WAIT_TIMEOUT:
                raise RuntimeError(f"Timed out waiting for payload index {field_name}")
            log.info("created payload index on %s.%s", index_name, field_name)
        except UnexpectedResponse as exc:
            # 409 conflict = already exists; anything else is real
            if exc.status_code != 409:
                raise
            current = client.get_collection(index_name).payload_schema.get(field_name)
            if not _index_matches(field_name, current, expected):
                raise RuntimeError(f"Payload index schema conflict for {field_name}") from exc
            log.debug("payload index on %s.%s already exists", index_name, field_name)
    if config.enable_sparse_embeddings and model_name == "baai/bge-m3" and not recorded_model:
        recorded = client.update_collection(
            collection_name=index_name, metadata={"nexrag_sparse_model": model_name}
        )
        if not recorded:
            raise RuntimeError("Could not record the sparse model for this collection")
    return True


def health_check() -> bool:
    """True iff Qdrant is reachable and answering. Used by /health/ready."""
    try:
        _raw_qdrant_client().get_collections()
        return True
    except Exception as exc:
        log.warning("qdrant health check failed: %s", exc)
        return False


def _ensure_sparse_layout(client, collection, config, index_name, active_ingest_version):
    # Earlier native installs created BM42's sparse index without its required
    # Qdrant-side IDF. Updating this modifier preserves all points and dense
    # vectors; a failed upgrade must propagate rather than claim readiness.
    collection_config = getattr(collection, "config", None)
    sparse = getattr(getattr(collection_config, "params", None), "sparse_vectors", None) or {}
    current = sparse.get("text-sparse")
    metadata = getattr(collection_config, "metadata", None)
    recorded_model = metadata.get("nexrag_sparse_model") if isinstance(metadata, dict) else None
    model_name = config.sparse_embedding_model.lower()
    if config.enable_sparse_embeddings and recorded_model and recorded_model != model_name:
        raise RuntimeError("Sparse model differs from the model recorded in this collection")
    if config.enable_sparse_embeddings and model_name == "baai/bge-m3" and not recorded_model:
        total = client.count(index_name, exact=True).count
        if total:
            if not active_ingest_version:
                raise RuntimeError("BGE-M3 requires a new or explicitly rebuilt sparse index")
            written = client.count(
                index_name,
                exact=True,
                count_filter=Filter(
                    must=[
                        FieldCondition(
                            key="meta.ingest_version",
                            match=MatchValue(value=active_ingest_version),
                        )
                    ]
                ),
            ).count
            if total != written:
                raise RuntimeError(
                    "Cannot mix new BGE-M3 vectors with unverified existing sparse vectors"
                )
    if (
        config.enable_sparse_embeddings
        and config.sparse_embedding_model.lower() == "baai/bge-m3"
        and current is not None
        and current.modifier == Modifier.IDF
    ):
        raise RuntimeError(
            "BGE-M3 requires a rebuilt sparse index; the existing collection uses IDF"
        )
    if (
        config.enable_sparse_embeddings
        and "bm42" in config.sparse_embedding_model.lower()
        and current is not None
        and current.modifier != Modifier.IDF
    ):
        client.update_collection(
            collection_name=index_name,
            sparse_vectors_config={"text-sparse": SparseVectorParams(modifier=Modifier.IDF)},
        )
        log.info("enabled BM42 IDF modifier on %s without rebuilding points", index_name)

    return model_name, recorded_model


def _payload_index_specs(config):
    # - meta.collection_name: the tenant index the multitenancy HNSW keys off.
    # - meta.collection_type: admin queries; not used for retrieval filtering.
    # - meta.languages: ISO 639-1 codes from converter language detection
    #   (Kreuzberg's ``detected_languages``). KEYWORD on a list-valued field
    #   supports MatchAny — perfect for "any of these languages". No consumer
    #   yet on the retrieval-agent side, but adding the index now means new
    #   ingests are searchable as soon as the filter call site lands.
    # - meta.file_id: every point op filters on it — the versioned overwrite
    #   sweep/teardown, DELETE /api/v1/documents/{file_id}, and the
    #   chunk-inspection count/scroll. Without the index those are full scans.
    # - meta.ingest_version: the versioned (blue/green) overwrite filters on it
    #   with must/must_not alongside file_id (see app/pipelines/indexing.py).
    tenant = KeywordIndexParams(type=KeywordIndexType.KEYWORD, is_tenant=True)
    keyword = PayloadSchemaType.KEYWORD
    indexes = [
        ("meta.collection_name", tenant, keyword),
        ("meta.collection_type", keyword, keyword),
        ("meta.languages", keyword, keyword),
        ("meta.file_id", keyword, keyword),
        ("meta.ingest_version", keyword, keyword),
    ]
    if config.enable_five_channel_retrieval or config.enable_graph_rag:
        text = TextIndexParams(
            type=TextIndexType.TEXT, tokenizer=TokenizerType.MULTILINGUAL, enable_hnsw=False
        )
        indexes += [
            ("content", text, PayloadSchemaType.TEXT),
            ("meta.filename", text, PayloadSchemaType.TEXT),
            ("meta.headers_breadcrumb", text, PayloadSchemaType.TEXT),
            ("meta.parent_split_id", PayloadSchemaType.INTEGER, PayloadSchemaType.INTEGER),
        ]
    return indexes
