"""Run integration acceptance with a real local model and disposable synthetic data.

This is not frontend acceptance. Copy no employee data, collections, credentials, or
production artifacts; create fresh evaluation stores under .local-run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.evaluation import datasets  # noqa: E402
from src.evaluation.runner import run_experiment  # noqa: E402
from src.evolution.artifacts import ArtifactRegistry  # noqa: E402
from src.evolution.cycle import EvolutionCycle  # noqa: E402
from src.memory.models import MemoryScope  # noqa: E402
from src.memory.runtime import memory_complete  # noqa: E402
from src.memory.service import MemoryService  # noqa: E402
from src.memory.vectors import HttpEmbedder, InMemoryVectorIndex  # noqa: E402
from src.midplatform.data_policy import is_loopback_endpoint  # noqa: E402
from src.midplatform.ops_store import OpsStore, set_ops_store  # noqa: E402


def create_fixture(directory: Path):
    """Point process-local policies, exports, and storage at fresh fixture paths and disable
    memory.
    """
    # This standalone CLI owns its operations store; per-case temporary adapters
    # never change production source paths.
    settings.TENANT_ACCESS_PATH = str(directory / "no-employee-policy.yaml")
    settings.EXPORTS_DIR = str(directory / "exports")
    settings.OPS_DB_PATH = str(directory / "ops.db")
    settings.ENABLE_MEMORY = False


async def verify_memory(store):
    """Exercise real extraction and local embeddings with an isolated in-memory vector index."""
    service = MemoryService(store, InMemoryVectorIndex(), HttpEmbedder(), completer=memory_complete)
    owner = MemoryScope("synthetic-memory-a", "synthetic-a", "synthetic-chat")
    other = MemoryScope("synthetic-memory-b", "synthetic-a", "synthetic-chat")
    await service.ingest_turn("我的回答语言偏好是中文，展示分析结果时我偏好使用表格。", owner)
    saved = service.store.list_for_user(owner.user_id)
    own_recall = await service.recall("语言和展示格式偏好", owner)
    other_recall = await service.recall("语言和展示格式偏好", other)
    forgotten = service.store.forget(owner.user_id)
    after_forget = await service.recall("语言和展示格式偏好", owner)
    return {
        "extracted_count": len(saved),
        "owner_recalled": bool(own_recall),
        "other_user_recalled": bool(other_recall),
        "forgotten_count": forgotten,
        "recall_after_forget": bool(after_forget),
        "passed": bool(saved) and bool(own_recall) and not other_recall and not after_forget,
    }


async def verify(args, directory):
    """Run synthetic evaluation, optional memory checks, and evolution against disposable stores."""
    settings.LLM_API_BASE = args.ollama_url.rstrip("/")
    settings.LLM_API_KEY = "ollama"
    settings.LLM_MODEL_NAME = args.model
    settings.EMBEDDING_API_BASE_URL = args.ollama_url.rstrip("/")
    settings.EMBEDDING_API_KEY = "ollama"
    settings.EMBEDDING_MODEL = args.embedding_model
    settings.QDRANT_URI = "http://127.0.0.1:6333"
    create_fixture(directory)
    store = OpsStore(settings.OPS_DB_PATH)
    set_ops_store(store)
    try:
        datasets.load_seed(
            Path(__file__).resolve().parents[1] / "evals/synthetic_local.jsonl", store
        )
        registry = ArtifactRegistry(store)
        prompt, params = registry.production("prompt"), registry.production("params")
        experiment = await run_experiment(
            "local-synthetic-acceptance",
            datasets.items(datasets.SPLITS, store),
            guidance=prompt.content,
            params=params.content,
            artifact_ids={"prompt": prompt.id, "params": params.id},
            record_synthetic_failures=True,
            store=store,
        )
        memory = await verify_memory(store) if args.with_memory else {"status": "not_requested"}
        # Mine and govern only this synthetic store; if eligible failures are too few,
        # do not claim an improvement or promotion.
        cycle = await EvolutionCycle(store=store, reg=registry).run()
        return {
            "model": args.model,
            "fixture_directory": str(directory),
            "metrics": experiment.metrics,
            "item_scores": experiment.item_scores,
            "source_unchanged": all(
                metrics.get("source_unchanged") == 1.0 for metrics in experiment.metrics.values()
            ),
            "memory": memory,
            "cycle": cycle.__dict__,
            "limits": [
                "Not frontend acceptance",
                "No elapsed real canary observation window",
                "No cloud credentials used",
                "Memory uses a temporary in-memory vector index",
            ],
        }
    finally:
        set_ops_store(None)
        store.close()


def main():
    """Require loopback model endpoints and return zero only for safe, high-quality,
    source-preserving runs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--embedding-model", default="bge-m3")
    parser.add_argument("--with-memory", action="store_true")
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    if not is_loopback_endpoint(args.ollama_url):
        parser.error("This synthetic acceptance command accepts only a loopback model endpoint")
    state = Path(__file__).resolve().parents[1] / ".local-run"
    state.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="agent-acceptance-", dir=state))
    report = asyncio.run(verify(args, directory))
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    metrics = report["metrics"].values()
    passed = report["source_unchanged"] and all(
        m.get("safety_violation", 0) == 0 and m.get("quality", 0) >= 0.999 for m in metrics
    )
    if args.with_memory:
        passed = passed and report["memory"]["passed"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
