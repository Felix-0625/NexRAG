"""轻量级原生解析器，父级上下文存放在子块 payload 中。

实现约定的 "Payload Trick"：细粒度子块用于稠密/稀疏检索建索引，
完整的父级上下文则嵌入 payload。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


class NativeParentChildChunker:
    """纯 Python 的层级文档解析与分块生成器。"""

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
        """尽量沿段落或句子边界切分文本。"""
        text = text.strip()
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]

        # 优先按双换行切分，其次单换行，再次句号/空格
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
                # 尽可能保留尾部作为重叠
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

        # 兜底：处理超过 chunk_size 的超长单段落
        final_chunks: list[str] = []
        for c in chunks:
            if len(c) > chunk_size * 1.5:
                # 按固定滑动窗口再切分
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
        """将原始文档切成父块，并把父块嵌入子块的 payload。"""
        content = content.strip()
        if not content:
            return []

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        effective_file_id = file_id or content_hash[:12]
        effective_collection = collection_name or "nexrag_docs"
        # 同一份字节上传到两个私有库时绝不能共用 point ID，
        # 否则 Qdrant upsert 会覆盖另一个库的 payload。
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
        """读取并解析本地 text/markdown/csv 文件，生成层级分块。"""
        path = Path(file_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)

        # 纯文本或常见 UTF-8 格式直接读取
        content = path.read_text(encoding="utf-8", errors="replace")
        return self.chunk_text(
            content,
            filename=path.name,
            tenant_id=tenant_id,
            file_id=file_id,
            collection_name=collection_name,
        )
