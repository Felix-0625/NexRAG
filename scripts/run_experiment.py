"""在评估数据集上评估当前线上（或指定）的产物配置。

示例：
    python scripts/run_experiment.py --seed evals/nexrag_seed.jsonl
    python scripts/run_experiment.py --splits holdout regression --repeats 2 --json-out docs/experiment.json
需要可访问的 LLM；文档类条目还需要检索服务。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.evaluation import datasets  # noqa: E402
from src.evaluation.runner import run_experiment  # noqa: E402
from src.evolution.artifacts import registry  # noqa: E402


def main() -> int:
    """载入种子数据后评估生产（或指定版本）的提示词与参数产物；任一拆分存在安全违规时返回 1，否则返回 0。"""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=Path("evals/nexrag_seed.jsonl"),
        help="JSONL seed to load first",
    )
    parser.add_argument(
        "--splits", nargs="+", default=list(datasets.SPLITS), choices=datasets.SPLITS
    )
    parser.add_argument("--prompt-version", help="Artifact ID to evaluate instead of production")
    parser.add_argument("--params-version", help="Artifact ID to evaluate instead of production")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--record-synthetic-failures",
        action="store_true",
        help="Record failures from isolated synthetic train items for RSI",
    )
    args = parser.parse_args()

    if args.seed and args.seed.exists():
        print(f"Loaded {datasets.load_seed(args.seed)} new seed item(s) from {args.seed}")
    prompt = (
        registry.get(args.prompt_version) if args.prompt_version else registry.production("prompt")
    )
    params = (
        registry.get(args.params_version) if args.params_version else registry.production("params")
    )
    if prompt is None or params is None:
        parser.error("Unknown artifact version")
    items = datasets.items(tuple(args.splits))
    if not items:
        parser.error("No dataset items for the selected splits")

    result = asyncio.run(
        run_experiment(
            "manual",
            items,
            guidance=prompt.content,
            params=params.content,
            artifact_ids={"prompt": prompt.id, "params": params.id},
            repeats=args.repeats,
            record_synthetic_failures=args.record_synthetic_failures,
        )
    )
    for split, metrics in result.metrics.items():
        shown = ", ".join(f"{k}={v:.3f}" for k, v in metrics.items() if k != "n")
        print(f"[{split}] n={int(metrics['n'])}  {shown}")
    print(f"Experiment run: {result.run_id}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                {"run_id": result.run_id, "metrics": result.metrics}, indent=2, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    worst = max((m.get("safety_violation", 0) for m in result.metrics.values()), default=0)
    return 1 if worst > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
