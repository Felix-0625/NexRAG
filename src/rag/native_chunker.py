"""Native parent-child parsing with whole parents embedded in child payloads.

Fine-grained children support dense and sparse indexing while payloads preserve context.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


class NativeParentChildChunker:
    """Pure-Python hierarchical document parser and chunk builder."""

    def __init__(
        self,
        parent_chunk_size: int = 1000,
        parent_overlap: int = 100,
        child_chunk_size: int = 200,
        child_overlap: int = 40,
    ):
        if child_chunk_size >= parent_chunk_size:
            raise ValueError("child_chunk_size must be smaller than parent_chunk_size")
        if not 0 <= parent_overlap < parent_chunk_size or not 0 <= child_overlap < child_chunk_size:
            raise ValueError("Chunk overlap must be non-negative and smaller than its chunk size")
        if min(parent_chunk_size, child_chunk_size) <= 0:
            raise ValueError("Chunk sizes must be positive")
        self.parent_chunk_size = parent_chunk_size
        self.parent_overlap = parent_overlap
        self.child_chunk_size = child_chunk_size
        self.child_overlap = child_overlap

    @staticmethod
    def _split_into_chunks(text: str, chunk_size: int, overlap: int) -> list[str]:
        """Prefer paragraph and sentence boundaries to retain readable context."""
        text = text.strip()
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]

        # Prefer semantic boundaries before spaces to preserve context.
        paragraphs = re.split(r"(\n\n+)", text)
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for part in paragraphs:
            if not part:
                continue
            if current_len + len(part) > chunk_size and current:
                joined = "".join(current).strip()
                if joined:
                    chunks.append(joined)
                # Retain overlap so boundaries do not lose context.
                overlap_text = joined[-overlap:] if overlap > 0 and len(joined) > overlap else ""
                current = [overlap_text, part] if overlap_text else [part]
                current_len = len(overlap_text) + len(part)
            else:
                current.append(part)
                current_len += len(part)

        if current:
            joined = "".join(current).strip()
            if joined:
                chunks.append(joined)

        # Bound oversized single paragraphs through fallback splitting.
        final_chunks: list[str] = []
        for c in chunks:
            if len(c) > chunk_size * 1.5:
                step = max(chunk_size - overlap, 50)
                for start in range(0, len(c), step):
                    sub = c[start : start + chunk_size].strip()
                    if sub:
                        final_chunks.append(sub)
            else:
                final_chunks.append(c)

        return final_chunks

    def chunk_text(
        self,
        content: str,
        filename: str,
        tenant_id: str | None = None,
        file_id: str | None = None,
        collection_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Create parent-child chunks with parent context inside child payloads."""
        content = content.strip()
        if not content:
            return []

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        effective_file_id = file_id or content_hash[:12]
        effective_collection = collection_name or "nexrag_docs"
        # Identical bytes in different private libraries must not share point IDs;
        # otherwise Qdrant upserts overwrite another library's payload.
        identity = f"{effective_collection}\0{effective_file_id}\0{content_hash}"
        doc_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        effective_tenant = tenant_id or "shared"

        parent_texts = self._split_into_chunks(content, self.parent_chunk_size, self.parent_overlap)
        results: list[dict[str, Any]] = []

        for p_idx, p_text in enumerate(parent_texts):
            parent_id = f"parent_{doc_hash}_{p_idx}"
            child_texts = self._split_into_chunks(p_text, self.child_chunk_size, self.child_overlap)

            for c_idx, c_text in enumerate(child_texts):
                child_id = f"child_{doc_hash}_{p_idx}_{c_idx}"
                meta_dict = {
                    "collection_name": effective_collection,
                    "parent_id": parent_id,
                    "parent_text": p_text,
                    "ingest_version": doc_hash,
                    "file_id": effective_file_id,
                    "source": filename,
                    "tenant_id": effective_tenant,
                    "chunk_index": c_idx,
                    "parent_index": p_idx,
                }
                results.append(
                    {
                        "id": child_id,
                        "text": c_text,
                        "payload": {
                            "content": c_text,
                            "text": c_text,
                            "parent_id": parent_id,
                            "parent_text": p_text,
                            "source": filename,
                            "file_id": effective_file_id,
                            "collection_name": effective_collection,
                            "tenant_id": effective_tenant,
                            "chunk_index": c_idx,
                            "parent_index": p_idx,
                            "meta": meta_dict,
                        },
                    }
                )

        return results

    def chunk_file(
        self,
        file_path: Path | str,
        tenant_id: str | None = None,
        file_id: str | None = None,
        collection_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read local text, Markdown, or CSV into hierarchical chunks."""
        path = Path(file_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)

        content = path.read_text(encoding="utf-8", errors="replace")
        return self.chunk_text(
            content,
            filename=path.name,
            tenant_id=tenant_id,
            file_id=file_id,
            collection_name=collection_name,
        )
