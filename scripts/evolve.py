"""运行一次批量自进化周期和记忆维护。

可通过计划任务/cron（如每天一次）调度，也可手动运行：
    python scripts/evolve.py                 # 挖掘 -> 提案 -> 门禁 -> 提交，金丝雀监控
    python scripts/evolve.py --memory-only   # 会话晋升、衰减、保留期清理、outbox 重放
这里不会晋升任何提案：提案留在管理后台等待管理员处理。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.evaluation import datasets  # noqa: E402
from src.evolution.cycle import EvolutionCycle  # noqa: E402
from src.memory.consolidate import decay_and_retain, promote_idle_sessions  # noqa: E402
from src.memory.runtime import get_memory_service, llm_complete  # noqa: E402
from src.observability.tracer import tracer  # noqa: E402


async def maintain_memory() -> dict:
    """执行记忆维护（空闲会话晋升、衰减与保留期清理、outbox 重放）并返回各项计数；记忆服务未启用时返回 disabled。"""
    service = get_memory_service()
    if service is None:
        return {"memory": "disabled"}
    return {
        "promoted_sessions": await promote_idle_sessions(service),
        **decay_and_retain(service),
        "outbox_replayed": await service.store.replay_outbox(service.embedder),
    }


async def main_async(args) -> dict:
    """先做记忆维护并清理过期 trace；除非指定 --memory-only，再载入种子数据并运行一次自进化周期，返回汇总结果。"""
    result: dict = {"memory": await maintain_memory(), "traces_purged": tracer.purge_expired()}
    if args.memory_only:
        return result
    if args.seed.exists():
        result["seed_items_added"] = datasets.load_seed(args.seed)
    result["cycle"] = (await EvolutionCycle(completer=llm_complete).run()).__dict__
    return result


def main() -> int:
    """解析命令行参数，运行自进化与记忆维护并以 JSON 打印结果，返回进程退出码 0。"""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--seed", type=Path, default=Path("evals/nexrag_seed.jsonl"))
    parser.add_argument("--memory-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main_async(args)), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
