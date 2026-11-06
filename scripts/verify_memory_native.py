"""验证真实的本地记忆抽取、嵌入和 Qdrant 持久化。

在 start.cmd 就绪后运行，且不要同时跑模型验收任务。只使用合成偏好数据。
每次运行都会新建唯一的 Qdrant collection 和独立的运维 SQLite 库，
绝不打开员工的记忆。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.memory.models import MemoryCandidate, MemoryScope  # noqa: E402
from src.memory.runtime import memory_complete  # noqa: E402
from src.memory.service import MemoryService  # noqa: E402
from src.memory.vectors import HttpEmbedder, QdrantVectorIndex  # noqa: E402
from src.midplatform.data_policy import is_loopback_endpoint  # noqa: E402
from src.midplatform.ops_store import OpsStore  # noqa: E402


def local_endpoint(value: str) -> str:
    """去掉末尾斜杠后返回端点；非回环地址或 URL 中带用户名密码时抛出 ValueError。"""
    endpoint = value.rstrip("/")
    parsed = urlsplit(endpoint)
    if not is_loopback_endpoint(endpoint) or parsed.username or parsed.password:
        raise ValueError("Only loopback endpoints without URL credentials are accepted")
    return endpoint


async def verify(args: argparse.Namespace) -> dict:
    """使用生产环境的记忆服务，搭配真实的 HTTP 嵌入和 Qdrant。"""
    endpoint = local_endpoint(args.ollama_url)
    qdrant = local_endpoint(args.qdrant_url)
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(endpoint + "/api/status")
        response.raise_for_status()
        if response.json().get("cloud", {}).get("disabled") is not True:
            raise RuntimeError("Ollama cloud-disabled status was not verified")

    # 这个独立验证器拥有这些进程内设置；线上应用的设置、运维库和记忆 collection
    # 都不受影响。
    settings.LLM_API_BASE = endpoint + "/v1"
    settings.LLM_API_KEY = "ollama"
    settings.LLM_MODEL_NAME = args.model
    settings.EMBEDDING_API_BASE_URL = endpoint + "/v1"
    settings.EMBEDDING_API_KEY = "ollama"
    settings.EMBEDDING_MODEL = args.embedding_model
    settings.QDRANT_URI = qdrant
    collection = "nexrag_acceptance_memory_" + uuid.uuid4().hex
    state = ROOT / ".local-run"
    state.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="memory-acceptance-", dir=state))
    database = directory / "ops.db"
    owner = MemoryScope("synthetic-memory-owner", "synthetic-a", "synthetic-chat")
    other = MemoryScope("synthetic-memory-other", "synthetic-a", "synthetic-chat")
    administrator = MemoryScope("synthetic-memory-admin", "synthetic-a", "synthetic-chat")
    index = QdrantVectorIndex(collection, url=qdrant)
    # 即使极小概率出现重名，也绝不复用已有 collection；
    # 清理范围仅限本次运行新建的命名空间。
    if index.client.collection_exists(collection):
        index.client.close()
        raise RuntimeError("Synthetic collection already exists; refusing to reuse it")
    store = OpsStore(str(database))
    checks: dict[str, bool] = {"cloud_disabled": True}
    try:
        service = MemoryService(
            store, index, HttpEmbedder(), completer=memory_complete, collection=collection
        )
        await service.ingest_turn("我的回答语言偏好是中文，展示分析结果时我偏好使用表格。", owner)
        extracted = service.store.list_for_user(owner.user_id)
        extracted_ids = {item.id for item in extracted}
        checks["real_structured_extraction"] = bool(extracted_ids)
        checks["owner_recalled"] = bool(await service.recall("语言和展示格式偏好", owner))
        # 用已存在的不同用户，在有数据的情况下验证过滤逻辑，
        # 而不是只证明空账户查不到 SQL 行。
        await service.write(
            MemoryCandidate(
                "global", "preference", "user", "language", "English", 1.0, "user_explicit", 7
            ),
            other,
        )
        expected_other = {item.id for item in service.store.list_for_user(other.user_id)}
        checks["other_user_has_independent_memory"] = bool(expected_other)
        other_recall = await service.recall("语言和展示格式偏好", other)
        checks["cross_user_denied"] = bool(other_recall) and all(
            item.record.id in expected_other for item in other_recall
        )
        checks["admin_identity_denied"] = not await service.recall(
            "语言和展示格式偏好", administrator
        )
        raw = index.client.scroll(collection, limit=100, with_payload=True, with_vectors=False)[0]
        checks["actual_qdrant_points"] = {p.payload["record_id"] for p in raw} == (
            extracted_ids | expected_other
        )
        checks["qdrant_payload_has_no_memory_text"] = all(
            set(p.payload)
            <= {"user_id", "tenant_id", "chat_id", "scope", "kind", "status", "record_id"}
            for p in raw
        )
        # 重建之前先销毁两个客户端和 SQLite 连接；
        # 新服务必须仅凭持久化存储就能恢复。
        index.client.close()
        store.close()
        index = QdrantVectorIndex(collection, url=qdrant)
        store = OpsStore(str(database))
        service = MemoryService(store, index, HttpEmbedder(), collection=collection)
        recalled = await service.recall("语言和展示格式偏好", owner)
        checks["reopened_service_recalled"] = bool(recalled) and all(
            item.record.id in extracted_ids for item in recalled
        )
        deleted = service.forget(owner.user_id)
        checks["forget_removed_sql_records"] = deleted == len(extracted_ids) > 0 and not (
            service.store.list_for_user(owner.user_id, include_inactive=True)
        )
        raw = index.client.scroll(collection, limit=100, with_payload=True, with_vectors=False)[0]
        checks["forget_removed_vectors_only_for_owner"] = {
            p.payload["record_id"] for p in raw
        } == expected_other
        checks["forgotten_not_recalled"] = not await service.recall("语言和展示格式偏好", owner)
        checks["other_memory_preserved"] = bool(await service.recall("language", other))
        checks["no_vector_outbox_failures"] = not store.query("SELECT * FROM vector_outbox")
        return {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "model": args.model,
            "embedding_model": args.embedding_model,
            "synthetic_only": True,
            "vector_index": "real Qdrant HTTP server",
            "extracted_count": len(extracted_ids),
            "checks": checks,
            "passed": all(checks.values()),
            "limits": [
                "Integration command, not frontend acceptance",
                "Service reconstruction, not an operating-system or Qdrant-server restart",
                "No elapsed long-term retention or production canary observation",
            ],
        }
    finally:
        store.close()
        try:
            if index.client.collection_exists(collection):
                index.client.delete_collection(collection)
        finally:
            index.client.close()


def main() -> int:
    """运行记忆验收并把报告写入 --json-out 与标准输出；验证抛出异常时记录为失败报告，全部通过返回 0，否则返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--embedding-model", default="bge-m3")
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(verify(args))
    except Exception as exc:
        report = {"passed": False, "synthetic_only": True, "error": f"{type(exc).__name__}: {exc}"}
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
