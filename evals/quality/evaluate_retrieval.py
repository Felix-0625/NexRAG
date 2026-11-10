"""Score real HTTP receipts by frozen spans; keep missing and unverifiable original questions."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from evals.quality.metrics import nearest_rank
from evals.quality.records import jsonl_line, read_jsonl
from evals.quality.spans import SpanScorer


def evaluate(dataset: Path, run: Path, output: Path, split: str) -> None:
    output.mkdir(parents=True, exist_ok=False)
    cases = read_jsonl(dataset / (split + ".jsonl"))
    receipts = read_jsonl(run / (split + "-retrieval.jsonl"))
    if len({row["id"] for row in receipts}) != len(receipts):
        raise ValueError("Duplicate actual HTTP receipts")
    by_id = {r["id"]: r for r in receipts}
    scorer = SpanScorer.from_files(dataset, run / "canonical-parents.json")
    scored = []
    for case in cases:
        receipt = by_id.get(case["id"])
        if (
            receipt
            and receipt.get("status") == 200
            and receipt.get("authorized")
            and "response" in receipt
        ):
            row = scorer.score(case, receipt["response"], receipt["source_ids"])
            row.update(receipt_kind="real-native-http", seconds=receipt["seconds"])
        else:
            row = {
                "id": case["id"],
                "cohort": case["cohort"],
                "source_group": case["group"],
                "split": split,
                "scores": {},
                "errors": {
                    m: "missing-failed-or-unauthorized-real-http-receipt"
                    for m in ("recall_at_10", "mrr", "ndcg_at_10")
                },
            }
        scored.append(row)
    with (output / "scores.jsonl").open("x", encoding="utf-8") as stream:
        for row in scored:
            stream.write(jsonl_line(row))
    summaries = []
    for cohort in ("zh-general", "en-medical", "en-legal"):
        rows = [r for r in scored if r["cohort"] == cohort]
        summary = {
            "cohort": cohort,
            "split": split,
            "planned": len(rows),
            "metrics": {},
            "formal_release_eligible": False,
            "boundary": "Real HTTP development observations; final holdout and full load are separate",
        }
        for metric in ("recall_at_10", "mrr", "ndcg_at_10"):
            values = [r["scores"][metric] for r in rows if metric in r["scores"]]
            summary["metrics"][metric] = {
                "observed_mean": statistics.mean(values) if values else None,
                "scored": len(values),
                "missing_or_unverifiable": len(rows) - len(values),
            }
        times = [r["seconds"] for r in rows if "seconds" in r]
        summary["diagnostic_serial_http_p95_seconds"] = nearest_rank(times, 0.95) if times else None
        summaries.append(summary)
    (output / "summary.json").write_text(json.dumps(summaries, indent=2), "utf-8")
    print(json.dumps(summaries), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "selection", "holdout"), default="train")
    args = parser.parse_args()
    evaluate(args.dataset, args.run, args.output, args.split)


if __name__ == "__main__":
    main()
