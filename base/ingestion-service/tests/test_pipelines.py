"""Factory selection and embedding failure invariants."""

import pytest

from app.pipelines.converters import build_converter
from app.pipelines.embedders import build_dense_embedder, build_sparse_embedder

from .pipeline_helpers import _settings

# -------------------- Converters --------------------


def test_build_converter_pypdf():
    s = _settings(extraction_engine="pypdf")
    c = build_converter(s)
    assert type(c).__name__ == "PyPDFToDocument"


def test_build_converter_kreuzberg():
    s = _settings(extraction_engine="kreuzberg")
    c = build_converter(s)
    assert type(c).__name__ == "KreuzbergRemoteConverter"


def test_build_converter_override_kreuzberg():
    """``engine_override=kreuzberg`` wins over the configured engine."""
    s = _settings(extraction_engine="pypdf")
    c = build_converter(s, engine_override="kreuzberg")
    assert type(c).__name__ == "KreuzbergRemoteConverter"


def test_build_converter_vision_llm():
    s = _settings(extraction_engine="vision-llm", vision_llm_api_base_url="http://vlm:8080/v1")
    c = build_converter(s)
    assert type(c).__name__ == "VisionLLMConverter"


def test_build_converter_vision_llm_threads_profile():
    s = _settings(
        extraction_engine="vision-llm",
        vision_llm_api_base_url="http://vlm:8080/v1",
        vision_llm_profile="ocr",
    )
    from app.pipelines.vision_llm_converter import VisionLLMConverter

    c = build_converter(s)
    assert isinstance(c, VisionLLMConverter)
    assert c._default_profile == "ocr"


def test_build_converter_vision_llm_invalid_profile_raises():
    s = _settings(
        extraction_engine="vision-llm",
        vision_llm_api_base_url="http://vlm:8080/v1",
        vision_llm_profile="banana",
    )
    with pytest.raises(ValueError, match="not a known profile"):
        build_converter(s)


def test_build_converter_unknown():
    s = _settings(extraction_engine="banana")
    with pytest.raises(ValueError, match="Unknown EXTRACTION_ENGINE"):
        build_converter(s)


def test_build_converter_for_pipeline_auto_returns_router():
    from app.pipelines.indexing import _build_converter_for_pipeline

    s = _settings(extraction_engine="auto", vision_llm_api_base_url="http://vlm:8080/v1")
    c = _build_converter_for_pipeline(s)
    assert type(c).__name__ == "RoutingConverter"


def test_build_converter_for_pipeline_concrete_engine():
    from app.pipelines.indexing import _build_converter_for_pipeline

    s = _settings(extraction_engine="kreuzberg")
    c = _build_converter_for_pipeline(s)
    assert type(c).__name__ == "KreuzbergRemoteConverter"


def test_build_converter_override_takes_precedence():
    """``engine_override`` (used by /api/v1/extract) wins over the configured engine."""
    s = _settings(extraction_engine="kreuzberg")
    c = build_converter(s, engine_override="pypdf")
    assert type(c).__name__ == "PyPDFToDocument"


def test_build_converter_override_validates_unknown():
    s = _settings(extraction_engine="kreuzberg")
    with pytest.raises(ValueError, match="Unknown EXTRACTION_ENGINE='banana'"):
        build_converter(s, engine_override="banana")


# -------------------- Dense embedders --------------------


def test_build_dense_openai_compat():
    s = _settings(embedding_provider="openai-compat")
    e = build_dense_embedder(s)
    assert type(e).__name__ == "OpenAIDocumentEmbedder"


def test_build_dense_tei_routes_to_openai_compat():
    s = _settings(embedding_provider="tei")
    e = build_dense_embedder(s)
    # TEI exposes an OpenAI-compatible endpoint, so we route through the same component.
    assert type(e).__name__ == "OpenAIDocumentEmbedder"


def test_build_dense_unknown():
    s = _settings(embedding_provider="banana")
    with pytest.raises(ValueError, match="Unknown EMBEDDING_PROVIDER"):
        build_dense_embedder(s)


def test_build_dense_sets_split_timeout_and_retries():
    """The HTTP budget must be *ours*, not Haystack's fallback.

    Haystack substitutes `timeout=30.0, max_retries=5` when these are left None
    (`OpenAIDocumentEmbedder._client_kwargs`), which absorbed only ~13s of
    downtime — a 2min blip on the embedding endpoint failed real uploads. The
    split connect/read timeout is what makes a longer retry budget affordable:
    a blackholed host costs 5s per attempt instead of 30s.
    """
    e = build_dense_embedder(_settings(embedding_provider="openai-compat"))

    assert e.timeout.connect == 5.0
    assert e.timeout.read == 30.0
    assert e.max_retries == 16
    # The values must survive Haystack's None-fallback logic all the way into
    # the kwargs handed to the OpenAI client — that fallback was the bug.
    kwargs = e._client_kwargs()
    assert kwargs["max_retries"] == 16
    assert kwargs["timeout"].connect == 5.0


def test_build_dense_http_budget_is_configurable():
    """Non-default values propagate (guards against hardcoded literals)."""
    e = build_dense_embedder(
        _settings(
            embedding_provider="tei",
            embedding_connect_timeout=2.5,
            embedding_read_timeout=45.0,
            embedding_max_retries=3,
        )
    )

    assert e.timeout.connect == 2.5
    assert e.timeout.read == 45.0
    # write/pool track connect so a half-open socket can't outlive it either.
    assert e.timeout.write == 2.5
    assert e.timeout.pool == 2.5
    assert e.max_retries == 3


def test_build_dense_embeds_headers_breadcrumb_by_default():
    """EMBED_HEADERS_BREADCRUMB defaults on: the embedder prepends
    meta.headers_breadcrumb to the text it encodes (stored content untouched)."""
    e = build_dense_embedder(_settings(embedding_provider="openai-compat"))
    assert e.meta_fields_to_embed == ["headers_breadcrumb"]


def test_build_dense_breadcrumb_opt_out():
    e = build_dense_embedder(
        _settings(embedding_provider="openai-compat", embed_headers_breadcrumb=False)
    )
    assert e.meta_fields_to_embed == []


# -------------------- Sparse embedders --------------------


def test_build_sparse_disabled_returns_none():
    s = _settings(enable_sparse_embeddings=False)
    assert build_sparse_embedder(s) is None


def test_build_sparse_provider_none_returns_none():
    s = _settings(enable_sparse_embeddings=True, sparse_embedding_provider="none")
    assert build_sparse_embedder(s) is None


def test_build_sparse_unknown_provider():
    s = _settings(enable_sparse_embeddings=True, sparse_embedding_provider="banana")
    with pytest.raises(ValueError, match="Unknown SPARSE_EMBEDDING_PROVIDER"):
        build_sparse_embedder(s)


def test_build_sparse_embeds_headers_breadcrumb_by_default():
    """The sparse embedder gets the breadcrumb too — heading terms boost
    BM42 keyword matching."""
    e = build_sparse_embedder(_settings(enable_sparse_embeddings=True))
    assert e.meta_fields_to_embed == ["headers_breadcrumb"]


def test_build_sparse_breadcrumb_opt_out():
    e = build_sparse_embedder(
        _settings(enable_sparse_embeddings=True, embed_headers_breadcrumb=False)
    )
    assert e.meta_fields_to_embed == []


# -------------------- Dense-embedding invariant --------------------


def test_dense_embedder_raises_on_failure():
    """A rejected batch must abort the run, not return unembedded documents."""
    e = build_dense_embedder(_settings(embedding_provider="openai-compat"))
    assert e.raise_on_failure is True


def test_embedding_guard_passes_embedded_documents():
    from haystack import Document

    from app.pipelines.embedders import DenseEmbeddingGuard

    docs = [Document(content="a", embedding=[0.1, 0.2]), Document(content="b", embedding=[0.3])]
    out = DenseEmbeddingGuard().run(documents=docs)["documents"]
    assert out == docs


def test_embedding_guard_rejects_missing_dense_vector():
    """Qdrant would store this point with only its sparse vector — invisible to
    vector search — under a 'successful' ingest. Fail instead."""
    from haystack import Document

    from app.pipelines.embedders import DenseEmbeddingGuard
    from app.pipelines.errors import EmbeddingError

    docs = [
        Document(content="ok", embedding=[0.1], meta={"split_id": 0}),
        Document(content="dropped", meta={"split_id": 1}),
    ]
    with pytest.raises(EmbeddingError, match="no dense embedding"):
        DenseEmbeddingGuard().run(documents=docs)


def test_embedding_guard_message_omits_chunk_text():
    """The message reaches logs and (with DEBUG) the API response."""
    from haystack import Document

    from app.pipelines.embedders import DenseEmbeddingGuard
    from app.pipelines.errors import EmbeddingError

    secret = "patient name and address"
    with pytest.raises(EmbeddingError) as excinfo:
        DenseEmbeddingGuard().run(documents=[Document(content=secret, meta={"split_id": 7})])
    assert secret not in str(excinfo.value)
    assert "7" in str(excinfo.value)
