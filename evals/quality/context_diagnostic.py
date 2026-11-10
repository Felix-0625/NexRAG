"""Measure whether frozen source spans survive into the actual generation context."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from evals.quality.generation_inputs import load_inputs
from evals.quality.records import read_jsonl
from evals.quality.spans import locations, searchable_source


def diagnose(dataset: Path, run: Path, split: str) -> dict:
    plan = dataset / f"{split}.jsonl"
    chat = run / f"{split}-chat.jsonl"
    http_digest = hashlib.sha256(chat.read_bytes()).hexdigest()
    cases = {row["id"]: row for row in read_jsonl(plan)}
    counts = defaultdict(Counter)
    results = []
    for row in load_inputs(dataset, run, split):
        case = cases[row["id"]]
        group = counts[case["cohort"]]
        group["http_receipts"] += 1
        result = {
            "id": row["id"],
            "cohort": row["cohort"],
            "status": row["http_receipt"].get("status"),
            "answer_origin": row.get("answer_origin"),
            "trace_sha256": row.get("trace_sha256"),
            "error": row["errors"],
        }
        if row["errors"]:
            group["unavailable"] += 1
            results.append(result)
            continue
        group[row["answer_origin"]] += 1
        passages = [
            passage
            for passage in row["passages"]
            if passage["authorized"] and passage["ingest_version"]
        ]
        group["with_verified_scope_context"] += bool(passages)
        source_ids = {passage["source_id"] for passage in passages}
        expected_sources = set(case["source_ids"])
        group["with_expected_source"] += bool(source_ids & expected_sources)
        normalized = {source: searchable_source(row["originals"][source]) for source in source_ids}
        intervals = [
            (passage["source_id"], locations(passage["text"], *normalized[passage["source_id"]]))
            for passage in passages
        ]
        spans = set()
        for span in case.get("spans", ()):
            text = span["text"]
            start = span["start"] + len(text) - len(text.lstrip())
            end = span["end"] - len(text) + len(text.rstrip())
            if start < end:
                spans.add((span["source_id"], start, end))
        covered = {
            (source, start, end)
            for source, start, end in spans
            if any(
                path == source and ranges and all(a <= start and end <= b for a, b in ranges)
                for path, ranges in intervals
            )
        }
        if spans:
            group["cases_with_official_spans"] += 1
            group["with_any_complete_span"] += bool(covered)
            group["with_all_complete_spans"] += covered == spans
        result.update(
            model_visible_passages=len(row["passages"]),
            authorized_versioned_passages=len(passages),
            source_ids=sorted(source_ids),
            expected_source_visible=bool(source_ids & expected_sources),
            official_spans=len(spans),
            complete_spans=sorted(covered),
        )
        results.append(result)
    if hashlib.sha256(chat.read_bytes()).hexdigest() != http_digest:
        raise ValueError("HTTP receipt file changed during diagnostic")
    return {
        "formal_quality_eligible": False,
        "boundary": "Exact source-span coverage in actual generation context, not Ragas Context Recall.",
        "case_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "http_receipts_sha256": http_digest,
        "cohorts": counts,
        "rows": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "selection", "holdout"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.dataset, args.run, args.split)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result["cohorts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
