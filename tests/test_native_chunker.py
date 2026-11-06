"""验证纯 Python 的原生父子分块与载荷嵌入。"""

from __future__ import annotations

from src.rag.native_chunker import NativeParentChildChunker


def test_native_chunker_hierarchical_payload():
    """验证子块在不依赖外部容器的情况下，完整嵌入父文本及其 id。"""
    chunker = NativeParentChildChunker(
        parent_chunk_size=300,
        parent_overlap=30,
        child_chunk_size=80,
        child_overlap=15,
    )

    doc_content = (
        "Section 1: Warranty and Returns.\n"
        "Customers must request refunds within 30 days of purchase.\n"
        "All original items, boxes, and receipts must be preserved.\n\n"
        "Section 2: Delivery Delays.\n"
        "In case of freight delay exceeding 5 business days, customers receive compensation.\n"
        "Disputes are submitted through the merchant portal and resolved within 48 hours."
    )

    chunks = chunker.chunk_text(doc_content, filename="policy.md", tenant_id="seller_test")
    assert len(chunks) > 0

    first_chunk = chunks[0]
    assert "id" in first_chunk
    assert "text" in first_chunk
    assert "payload" in first_chunk

    payload = first_chunk["payload"]
    assert "parent_id" in payload
    assert "parent_text" in payload
    assert payload["source"] == "policy.md"
    assert payload["tenant_id"] == "seller_test"

    # 验证子块文本包含在嵌入的父文本之内
    assert first_chunk["text"] in payload["parent_text"]
    assert len(payload["parent_text"]) >= len(first_chunk["text"])


def test_native_chunker_empty_input():
    """空文本或空白文本不应产生任何分块。"""
    chunker = NativeParentChildChunker(parent_chunk_size=500, child_chunk_size=100)
    assert chunker.chunk_text("", "empty.txt") == []
    assert chunker.chunk_text("   \n\n  ", "blank.txt") == []


def test_identical_documents_in_distinct_libraries_do_not_overwrite_each_other():
    chunker = NativeParentChildChunker()
    a = chunker.chunk_text(
        "same content", "same.txt", file_id="same-file", collection_name="private-a"
    )
    b = chunker.chunk_text(
        "same content", "same.txt", file_id="same-file", collection_name="private-b"
    )
    c = chunker.chunk_text(
        "same content", "same.txt", file_id="another-file", collection_name="private-a"
    )
    assert len({a[0]["id"], b[0]["id"], c[0]["id"]}) == 3
    assert a == chunker.chunk_text(
        "same content", "same.txt", file_id="same-file", collection_name="private-a"
    )


def test_native_chunker_file_parsing(tmp_path):
    """对真实的临时 markdown 文件分块能够成功。"""
    test_file = tmp_path / "guide.md"
    test_file.write_text("# Guide\nThis is a test guide for native chunking.", encoding="utf-8")

    chunker = NativeParentChildChunker(parent_chunk_size=200, child_chunk_size=50)
    chunks = chunker.chunk_file(test_file)
    assert len(chunks) >= 1
    assert chunks[0]["payload"]["source"] == "guide.md"
