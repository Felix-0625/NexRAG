"""把本地文档索引到 NexRAG 的共享 Agent 知识库集合。

支持两种模式：
1. 原生流水线（--native）：轻量的纯 Python 解析，采用父子层级分块
   （"Payload Trick"），通过 OpenAI 兼容 API（Ollama/vLLM）直接生成嵌入，
   并在本地原生进程内写入 Qdrant。
2. 旧版上传模式（省略 --native 时的默认模式）：通过 HTTP 把文件分发给上游
   AarhusAI 摄取服务。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from dotenv import dotenv_values

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.rag.native_chunker import NativeParentChildChunker


def _documents(path: Path) -> list[tuple[Path, str]]:
    """返回文件及可读的相对路径标签，供上传报告使用。"""
    if not path.exists():
        raise FileNotFoundError(path)
    root = path if path.is_dir() else path.parent
    files = sorted(item for item in path.rglob("*") if item.is_file()) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"No files found under {path}")
    return [(item, item.relative_to(root).as_posix()) for item in files]


def _file_id(path: Path, collection: str) -> str:
    """让重复上传的 ID 保持稳定，同时不合并来自两个文件夹的同名文件。"""
    identity = f"{collection}\0{path.resolve()}"
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


# base/retrieval-agent/app/services/qdrant.py 约定的 Qdrant 向量名。该服务只有在
# collection 含具名 sparse 向量时才走混合 RRF 路径；此时稠密向量也必须具名
# （同一个 Qdrant collection 中，未命名的默认向量与具名 sparse 向量不能共存）。
_DENSE_VECTOR_NAME = "text-dense"
_SPARSE_VECTOR_NAME = "text-sparse"


def _embed_texts(
    texts: list[str],
    api_base_url: str,
    api_key: str,
    model: str,
) -> list[list[float]]:
    """通过 OpenAI 兼容的 /embeddings 端点生成文本嵌入。"""
    if not texts:
        return []
    url = f"{api_base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            url,
            json={"model": model, "input": texts},
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        sorted_items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in sorted_items]


def _embed_sparse(texts: list[str], model_name: str) -> list[Any]:
    """用本地 FastEmbed BM42 稀疏模型生成文本嵌入。

    每段文本返回一个 ``qdrant_client`` 的 ``SparseVector``，与
    base/retrieval-agent 在查询侧稀疏嵌入时加载的模型一致
    （``SPARSE_MODEL`` / ``SPARSE_QUERY_MODEL``，默认
    ``Qdrant/bm42-all-minilm-l6-v2-attentions``）。模型权重在首次使用时下载并缓存。
    """
    from fastembed import SparseTextEmbedding
    from qdrant_client.models import SparseVector

    model = SparseTextEmbedding(model_name=model_name)
    return [
        SparseVector(indices=vec.indices.tolist(), values=vec.values.tolist())
        for vec in model.embed(texts)
    ]


def _ensure_collection(client: Any, collection_name: str, dim: int, use_sparse: bool) -> None:
    """collection 不存在时创建：具名稠密向量，需要时附带具名稀疏向量（IDF 修正）。"""
    from qdrant_client.models import Distance, Modifier, SparseVectorParams, VectorParams

    if not client.collection_exists(collection_name):
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                _DENSE_VECTOR_NAME: VectorParams(size=dim, distance=Distance.COSINE),
            },
            sparse_vectors_config=(
                {_SPARSE_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF)}
                if use_sparse
                else None
            ),
        )


def _build_points(
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
    sparse_embeddings: list[Any] | None,
) -> list[Any]:
    """把分块与其稠密（及可选稀疏）向量组装成 Qdrant 点，点 ID 由分块 ID 稳定派生。"""
    from qdrant_client.models import PointStruct

    if sparse_embeddings is not None:
        return [
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_DNS, chunk["id"])),
                vector={_DENSE_VECTOR_NAME: dense, _SPARSE_VECTOR_NAME: sparse},
                payload=chunk["payload"],
            )
            for chunk, dense, sparse in zip(chunks, embeddings, sparse_embeddings, strict=True)
        ]
    return [
        PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, chunk["id"])),
            vector={_DENSE_VECTOR_NAME: dense},
            payload=chunk["payload"],
        )
        for chunk, dense in zip(chunks, embeddings, strict=True)
    ]


def _upsert_in_batches(client: Any, collection_name: str, points: list[Any]) -> None:
    """按固定批大小写入，并等待每批落盘确认。"""
    batch_size = 64
    for i in range(0, len(points), batch_size):
        client.upsert(
            collection_name=collection_name,
            points=points[i : i + batch_size],
            wait=True,
        )


def _delete_stale_versions(client: Any, collection_name: str, chunks: list[dict[str, Any]]) -> None:
    """清除每个（知识范围, 文件 ID）下除当前 ingest_version 以外的旧版本点。"""
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

    # 只有在每个新批次都被确认写入后，才清除过期版本。逻辑知识范围和文件标识
    # 缺一不可：别的知识库复用同一个文件 ID 时，绝不能删掉该知识库的点。
    versions = {
        (
            chunk["payload"]["meta"]["collection_name"],
            chunk["payload"]["meta"]["file_id"],
            chunk["payload"]["meta"]["ingest_version"],
        )
        for chunk in chunks
    }
    for scope, file_id, version in versions:
        client.delete(
            collection_name=collection_name,
            wait=True,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(key="meta.collection_name", match=MatchValue(value=scope)),
                        FieldCondition(key="meta.file_id", match=MatchValue(value=file_id)),
                    ],
                    must_not=[
                        FieldCondition(key="meta.ingest_version", match=MatchValue(value=version))
                    ],
                )
            ),
        )


def _upsert_to_qdrant(
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
    sparse_embeddings: list[Any] | None,
    qdrant_url: str,
    qdrant_api_key: str | None,
    collection_name: str,
) -> None:
    """把分块的稠密（及可选稀疏）嵌入和 payload 写入 Qdrant。

    传入 ``sparse_embeddings`` 时，创建带具名稠密向量（``text-dense``）和
    稀疏向量（``text-sparse``）的 collection，使 base/retrieval-agent 的混合 RRF
    路径自动接管（见模块级向量名常量）。``sparse_embeddings`` 为 ``None``
    （``--no-sparse``）时，退回只含稠密具名向量；检索随之平滑降级为纯稠密检索。
    """
    from qdrant_client import QdrantClient

    if not chunks or len(chunks) != len(embeddings):
        raise ValueError("Every child chunk must have one dense vector")
    if sparse_embeddings is not None and len(sparse_embeddings) != len(chunks):
        raise ValueError("Every child chunk must have one sparse vector")
    client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
    dim = len(embeddings[0])
    _ensure_collection(client, collection_name, dim, sparse_embeddings is not None)
    points = _build_points(chunks, embeddings, sparse_embeddings)
    _upsert_in_batches(client, collection_name, points)
    _delete_stale_versions(client, collection_name, chunks)
    client.close()


class _NativeSettings(NamedTuple):
    """原生摄取用到的、已按"命令行 > 环境变量 > .env > 默认值"解析好的配置。"""

    embedding_base: str
    embedding_key: str
    embedding_model: str
    qdrant_url: str
    qdrant_index: str
    sparse_model: str


def _setting(cli_value: Any, env_name: str, local_env: dict[str, str | None], default: str) -> Any:
    return cli_value or os.getenv(env_name) or local_env.get(env_name) or default


def _native_settings(args: argparse.Namespace, local_env: dict[str, str | None]) -> _NativeSettings:
    return _NativeSettings(
        embedding_base=_setting(
            args.embedding_url,
            "EMBEDDING_API_BASE_URL",
            local_env,
            "http://localhost:11434/v1",
        ),
        embedding_key=_setting(args.embedding_key, "EMBEDDING_API_KEY", local_env, "ollama"),
        embedding_model=_setting(args.embedding_model, "EMBEDDING_MODEL", local_env, "bge-m3"),
        qdrant_url=_setting(args.qdrant_url, "QDRANT_URI", local_env, "http://localhost:6333"),
        qdrant_index=_setting(args.qdrant_index, "QDRANT_INDEX", local_env, "nexrag_knowledge"),
        sparse_model=_setting(
            args.sparse_model,
            "SPARSE_MODEL",
            local_env,
            "Qdrant/bm42-all-minilm-l6-v2-attentions",
        ),
    )


def _embed_and_index(
    label: str,
    chunks: list[dict[str, Any]],
    args: argparse.Namespace,
    local_env: dict[str, str | None],
    settings: _NativeSettings,
) -> None:
    """嵌入子块、写入 Qdrant 并打印索引结果。"""
    # 嵌入子块：稠密向量走 OpenAI 兼容端点，稀疏向量用本地 FastEmbed BM42
    # 模型（除非禁用），使原生摄取的文档与旧版 AarhusAI 流水线摄取的文档一样，
    # 可通过 base/retrieval-agent 的混合 RRF 路径检索到。
    child_texts = [c["text"] for c in chunks]
    embeddings = _embed_texts(
        child_texts,
        api_base_url=settings.embedding_base,
        api_key=settings.embedding_key,
        model=settings.embedding_model,
    )
    sparse_embeddings = (
        None if args.no_sparse else _embed_sparse(child_texts, settings.sparse_model)
    )

    # 直接写入 Qdrant
    _upsert_to_qdrant(
        chunks=chunks,
        embeddings=embeddings,
        sparse_embeddings=sparse_embeddings,
        qdrant_url=settings.qdrant_url,
        qdrant_api_key=os.getenv("QDRANT_API_KEY") or local_env.get("QDRANT_API_KEY"),
        collection_name=settings.qdrant_index,
    )
    hybrid_note = "dense-only" if args.no_sparse else "hybrid dense+BM42"
    print(
        f"{label}: {len(chunks)} chunks indexed into '{settings.qdrant_index}' "
        f"({hybrid_note}, scope: {args.collection})"
    )


def _ingest_native(
    files: list[tuple[Path, str]],
    args: argparse.Namespace,
    local_env: dict[str, str | None],
) -> int:
    """执行不依赖容器的原生纯 Python 摄取。"""
    chunker = NativeParentChildChunker(
        parent_chunk_size=args.parent_size,
        child_chunk_size=args.child_size,
    )
    all_chunks: list[dict[str, Any]] = []
    failures = 0
    settings = _native_settings(args, local_env)

    for path, label in files:
        try:
            file_id = _file_id(path, args.collection)
            chunks = chunker.chunk_file(
                path,
                file_id=file_id,
                collection_name=args.collection,
            )
            if not chunks:
                print(f"{label}: no text content found (skipped)")
                continue

            all_chunks.extend(chunks)

            if args.dry_run:
                print(f"{label}: parsed {len(chunks)} chunks (dry-run)")
                continue

            _embed_and_index(label, chunks, args, local_env, settings)
        except Exception as exc:
            failures += 1
            print(f"{label}: native ingestion failed: {exc}", file=sys.stderr)

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.write_text(json.dumps(all_chunks, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Exported {len(all_chunks)} chunks to {out_path}")

    return 1 if failures else 0


def _ingest_legacy(
    files: list[tuple[Path, str]],
    args: argparse.Namespace,
    api_key: str,
) -> int:
    """通过 HTTP 把文件分发给原生 AarhusAI 摄取服务。"""
    failures = 0
    with httpx.Client(timeout=300.0) as client:
        for path, label in files:
            fields = {
                "file_id": _file_id(path, args.collection),
                "filename": path.name,
                "collection_name": args.collection,
                "collection_type": "knowledge",
                "user_id": "nexrag-local",
                "overwrite": "true",
            }
            try:
                with path.open("rb") as handle:
                    response = client.put(
                        f"{args.url.rstrip('/')}/api/v1/ingest",
                        headers={"Authorization": f"Bearer {api_key}"},
                        data=fields,
                        files={"file": (path.name, handle)},
                    )
                response.raise_for_status()
                payload = response.json()
                print(f"{label}: {payload['chunks_count']} chunks")
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                failures += 1
                print(f"{label}: legacy ingestion failed: {exc}", file=sys.stderr)
    return 1 if failures else 0


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    """文档路径与运行模式相关的命令行参数。"""
    parser.add_argument("path", type=Path, help="Document file or directory")
    parser.add_argument(
        "--native",
        action="store_true",
        help="Use pure-Python native parent-child chunking and direct Qdrant indexing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and display chunking statistics without sending embeddings or writing to Qdrant",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Path to save parsed chunks as a JSON file",
    )
    parser.add_argument(
        "--parent-size",
        type=int,
        default=1000,
        help="Parent chunk character limit for LLM context",
    )
    parser.add_argument(
        "--child-size",
        type=int,
        default=200,
        help="Child chunk character limit for retrieval embeddings",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8001",
        help="Legacy AarhusAI ingestion service URL (ignored in --native mode)",
    )


def _add_embedding_arguments(parser: argparse.ArgumentParser, local_env: dict) -> None:
    """Qdrant、嵌入模型、稀疏模型与逻辑 collection 相关的命令行参数。"""
    parser.add_argument(
        "--qdrant-url",
        default=None,
        help="Qdrant endpoint for native indexing (default: http://localhost:6333)",
    )
    parser.add_argument(
        "--qdrant-index",
        default=None,
        help="Physical Qdrant collection name (default: nexrag_knowledge)",
    )
    parser.add_argument(
        "--embedding-url",
        default=None,
        help="OpenAI-compatible embedding base URL (default: http://localhost:11434/v1)",
    )
    parser.add_argument(
        "--embedding-model",
        default=None,
        help="Embedding model identifier (default: bge-m3)",
    )
    parser.add_argument(
        "--embedding-key",
        default=None,
        help="Embedding API authorization key (default: ollama)",
    )
    parser.add_argument(
        "--sparse-model",
        default=None,
        help=(
            "FastEmbed BM42 sparse model for native indexing "
            "(default: Qdrant/bm42-all-minilm-l6-v2-attentions, matching "
            "base/retrieval-agent's query-side model)"
        ),
    )
    parser.add_argument(
        "--no-sparse",
        action="store_true",
        help=(
            "Skip BM42 sparse embeddings in --native mode and index dense-only. "
            "Retrieval still works but without hybrid RRF fusion."
        ),
    )
    parser.add_argument(
        "--collection",
        default=os.getenv("KNOWLEDGE_COLLECTION_NAME")
        or local_env.get("KNOWLEDGE_COLLECTION_NAME")
        or "nexrag_docs",
        help="Logical collection name for metadata scoping",
    )


def main() -> int:
    """上传文档，或以原生方式把文档索引进 Qdrant。"""
    local_env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    _add_input_arguments(parser)
    _add_embedding_arguments(parser, local_env)
    args = parser.parse_args()

    try:
        files = _documents(args.path.resolve())
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    if args.native:
        return _ingest_native(files, args, local_env)

    # 旧版模式需要 INGESTION_API_KEY
    api_key = os.getenv("INGESTION_API_KEY") or local_env.get("INGESTION_API_KEY")
    if not api_key or len(api_key) < 32:
        parser.error(
            "Legacy ingestion requires INGESTION_API_KEY (>=32 chars). "
            "Use --native for lightweight pure-Python indexing without containers."
        )

    return _ingest_legacy(files, args, api_key)


if __name__ == "__main__":
    raise SystemExit(main())
