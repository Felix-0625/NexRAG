"""Document upload identifiers used by local knowledge ingestion."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from scripts.ingest_documents import _documents, _file_id, _ingest_native


def test_reupload_keeps_id_and_same_named_files_do_not_collide(tmp_path: Path):
    """Two teams can import guide.pdf without overwriting each other."""
    first = tmp_path / "team-a" / "guide.pdf"
    second = tmp_path / "team-b" / "guide.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    assert _file_id(first, "nexrag_docs") == _file_id(first, "nexrag_docs")
    assert _file_id(first, "nexrag_docs") != _file_id(second, "nexrag_docs")
    assert _documents(first.parent) == [(first, "guide.pdf")]


def test_native_ingest_dry_run_and_json_export(tmp_path: Path):
    """Native dry-run chunks text and exports JSON."""
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("Hello world.\n\nThis is a test document.", encoding="utf-8")
    out_json = tmp_path / "exported.json"

    args = argparse.Namespace(
        path=doc_path,
        collection="test_collection",
        parent_size=500,
        child_size=100,
        dry_run=True,
        output_json=str(out_json),
        embedding_url="http://localhost:11434/v1",
        embedding_key="ollama",
        embedding_model="bge-m3",
        qdrant_url="http://localhost:6333",
        qdrant_index="nexrag_knowledge",
        sparse_model=None,
        no_sparse=False,
    )

    exit_code = _ingest_native([(doc_path, "doc.txt")], args, {})
    assert exit_code == 0
    assert out_json.exists()

    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(data) > 0
    assert "parent_id" in data[0]["payload"]
    assert "parent_text" in data[0]["payload"]
    assert data[0]["payload"]["collection_name"] == "test_collection"


def test_native_ingest_embeds_and_upserts(tmp_path: Path):
    """Native ingestion embeds children and upserts Qdrant."""
    doc_path = tmp_path / "article.md"
    doc_path.write_text("Paragraph one.\n\nParagraph two.", encoding="utf-8")

    args = argparse.Namespace(
        path=doc_path,
        collection="test_collection",
        parent_size=500,
        child_size=100,
        dry_run=False,
        output_json=None,
        embedding_url="http://localhost:11434/v1",
        embedding_key="ollama",
        embedding_model="bge-m3",
        qdrant_url="http://localhost:6333",
        qdrant_index="nexrag_knowledge",
        sparse_model=None,
        no_sparse=False,
    )

    with (
        patch("scripts.ingest_documents._embed_texts", return_value=[[0.1, 0.2]]) as mock_embed,
        patch("scripts.ingest_documents._embed_sparse", return_value=["sparse-vec"]) as mock_sparse,
        patch("scripts.ingest_documents._upsert_to_qdrant") as mock_upsert,
    ):
        exit_code = _ingest_native([(doc_path, "article.md")], args, {})
        assert exit_code == 0
        assert mock_embed.called
        assert mock_sparse.called
        assert mock_upsert.called
        # Sparse embeddings must reach upsert because hybrid retrieval is the default.
        assert mock_upsert.call_args.kwargs["sparse_embeddings"] == ["sparse-vec"]


def test_native_ingest_no_sparse_flag_skips_bm42(tmp_path: Path):
    """--no-sparse skips BM42 and writes dense vectors only."""
    doc_path = tmp_path / "article.md"
    doc_path.write_text("Paragraph one.\n\nParagraph two.", encoding="utf-8")

    args = argparse.Namespace(
        path=doc_path,
        collection="test_collection",
        parent_size=500,
        child_size=100,
        dry_run=False,
        output_json=None,
        embedding_url="http://localhost:11434/v1",
        embedding_key="ollama",
        embedding_model="bge-m3",
        qdrant_url="http://localhost:6333",
        qdrant_index="nexrag_knowledge",
        sparse_model=None,
        no_sparse=True,
    )

    with (
        patch("scripts.ingest_documents._embed_texts", return_value=[[0.1, 0.2]]),
        patch("scripts.ingest_documents._embed_sparse") as mock_sparse,
        patch("scripts.ingest_documents._upsert_to_qdrant") as mock_upsert,
    ):
        exit_code = _ingest_native([(doc_path, "article.md")], args, {})
        assert exit_code == 0
        assert not mock_sparse.called
        assert mock_upsert.call_args.kwargs["sparse_embeddings"] is None


def test_native_revision_cleanup_preserves_other_libraries(monkeypatch):
    """Verify real local Qdrant upsert and delete semantics with isolated data."""
    from qdrant_client import QdrantClient, models

    from scripts.ingest_documents import _upsert_to_qdrant
    from src.rag.native_chunker import NativeParentChildChunker

    client = QdrantClient(":memory:")
    real_close = client.close
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda **kwargs: client)
    chunker = NativeParentChildChunker()

    def ingest(text, scope):
        """Provide ingest test data or behavior for this scenario."""
        chunks = chunker.chunk_text(text, "doc.txt", file_id="same-file", collection_name=scope)
        _upsert_to_qdrant(chunks, [[0.1, 0.2] for _ in chunks], None, "unused", None, "test")

    try:
        ingest("old private A content", "private-a")
        ingest("private B remains unchanged", "private-b")
        ingest("new private A content", "private-a")
        points, _ = client.scroll("test", limit=10, with_payload=True)
        assert len(points) == 2
        by_scope = {
            point.payload["meta"]["collection_name"]: point.payload["content"] for point in points
        }
        assert by_scope == {
            "private-a": "new private A content",
            "private-b": "private B remains unchanged",
        }
        assert all(point.payload["meta"]["parent_text"] for point in points)
        # A separate hybrid collection uses server-side IDF, not client text scoring.
        chunks = chunker.chunk_text("hybrid source", "hybrid.txt", collection_name="hybrid")
        _upsert_to_qdrant(
            chunks,
            [[0.1, 0.2]],
            [models.SparseVector(indices=[1], values=[0.5])],
            "unused",
            None,
            "hybrid-test",
        )
        assert (
            client.get_collection("hybrid-test")
            .config.params.sparse_vectors["text-sparse"]
            .modifier
            == models.Modifier.IDF
        )
    finally:
        real_close()
