"""Run published FinQA questions through the live retrieval API and Ragas.

Only the dedicated public benchmark library is queried. This is service-level
retrieval evidence, not a new frontend answer or a numeric-reasoning evaluation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

import httpx
from dotenv import dotenv_values
from ragas.dataset_schema import SingleTurnSample
from ragas.metrics import IDBasedContextRecall

ROOT = Path(__file__).resolve().parents[2]


async def main(fixtures: Path, library: str) -> None:
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf8"))
    cases = manifest["cases"]
    # FinQA supplies a source report with each question. Preserve that input
    # scope instead of treating underspecified questions as corpus-wide facts.
    queries = [f"Report {c['source_report']}: {c['question']}" for c in cases]
    environment = dotenv_values(ROOT / ".env")
    runtime = json.loads((ROOT / ".local-run/runtime.json").read_text(encoding="utf8"))
    endpoint = f"http://127.0.0.1:{runtime['ports']['retrieval']}/search"
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {environment['RETRIEVAL_API_KEY']}"},
            json={"queries": queries, "collection_names": [library], "k": 3},
        )
        response.raise_for_status()
    payload = response.json()
    assert len(payload["documents"]) == len(cases), "Incomplete batch response"
    (fixtures / "retrieval-raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    scorer = IDBasedContextRecall()
    results = []
    for case, query, texts, metadata in zip(
        cases, queries, payload["documents"], payload["metadatas"], strict=True
    ):
        # The filename is ingestion metadata, not inferred from the answer.
        retrieved = [Path(m.get("source") or m.get("filename") or "").name for m in metadata]
        sample = SingleTurnSample(
            retrieved_context_ids=retrieved, reference_context_ids=[case["filename"]]
        )
        recall = await scorer.single_turn_ascore(sample)
        results.append(
            {
                "id": case["id"],
                "query": query,
                "expected_file": case["filename"],
                "retrieved_files": retrieved,
                "parent_count": len(texts),
                "recall_at_3": recall,
            }
        )
    mean = sum(r["recall_at_3"] for r in results) / len(results)
    report = {
        "framework": "Ragas 0.4.3 IDBasedContextRecall",
        "library_id": library,
        "scope": "Public financial-domain document retrieval smoke benchmark",
        "input_scope": "Published source-report identity plus original question",
        "seconds": round(time.monotonic() - started, 3),
        "cases": results,
        "mean_document_recall_at_3": mean,
        "predeclared_minimum": manifest["predeclared_checks"]["document_recall_at_3_min_mean"],
        "passed": mean >= manifest["predeclared_checks"]["document_recall_at_3_min_mean"],
        "answer_correctness_evaluated": False,
    }
    (fixtures / "retrieval-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--library", required=True, help="Dedicated public benchmark library ID")
    args = parser.parse_args()
    asyncio.run(main(args.fixtures, args.library))
