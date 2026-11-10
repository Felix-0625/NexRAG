"""Run one batch evolution cycle and memory maintenance.

Schedule daily or run manually; --memory-only skips evolution. Proposals remain in the
administration queue and are never promoted here.
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
    """Maintain idle sessions, decay, retention, and vector replay; return counts or disabled
    status.
    """
    service = get_memory_service()
    if service is None:
        return {"memory": "disabled"}
    return {
        "promoted_sessions": await promote_idle_sessions(service),
        **decay_and_retain(service),
        "outbox_replayed": await service.store.replay_outbox(service.embedder),
    }


async def main_async(args) -> dict:
    """Maintain memory and traces, then optionally seed datasets and run evolution."""
    result: dict = {"memory": await maintain_memory(), "traces_purged": tracer.purge_expired()}
    if args.memory_only:
        return result
    if args.seed.exists():
        result["seed_items_added"] = datasets.load_seed(args.seed)
    result["cycle"] = (await EvolutionCycle(completer=llm_complete).run()).__dict__
    return result


def main() -> int:
    """Parse options, run maintenance and evolution, print JSON, and return zero."""
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
