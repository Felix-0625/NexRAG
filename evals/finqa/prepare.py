"""Prepare a fixed public FinQA smoke set without model-generated answers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx

COMMIT = "0f16e2867befa6840783e58be38c9efb9229d742"
DATASET_SHA256 = "831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc"
BASE = f"https://raw.githubusercontent.com/czyssrs/FinQA/{COMMIT}"


def prepare(destination: Path) -> None:
    corpus = destination / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.get(f"{BASE}/dataset/test.json")
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != DATASET_SHA256:
            raise ValueError("Published FinQA fixture checksum changed")
        license_response = client.get(f"{BASE}/LICENSE")
        license_response.raise_for_status()
    (destination / "test.json").write_bytes(response.content)
    (destination / "LICENSE").write_bytes(license_response.content)
    selected, seen = [], set()
    for item in response.json():
        if item["filename"] in seen:
            continue
        seen.add(item["filename"])
        filename = f"finqa-{len(selected) + 1:02d}.md"
        table = "\n".join(
            "| " + " | ".join(str(cell).replace("|", "/") for cell in row) + " |"
            for row in item["table"]
        )
        report = "\n\n".join(
            [
                "# Public FinQA source: " + item["filename"],
                "Source report ID: " + item["id"],
                "\n".join(item["pre_text"]),
                table,
                "\n".join(item["post_text"]),
            ]
        )
        (corpus / filename).write_text(report, encoding="utf8")
        selected.append(
            {
                "id": item["id"],
                "filename": filename,
                "source_report": item["filename"],
                "question": item["qa"]["question"],
                "reference_answer": item["qa"]["answer"],
                "reference_program": item["qa"]["program"],
                "reference_exe_answer": item["qa"].get("exe_ans"),
                "reference_evidence": item["qa"]["gold_inds"],
            }
        )
        if len(selected) == 10:
            break
    manifest = {
        "source": "https://github.com/czyssrs/FinQA",
        "commit": COMMIT,
        "split": "test",
        "selection": "First 10 unique source-report filenames in published test order; selected before retrieval/model results",
        "dataset_sha256": DATASET_SHA256,
        "cases": selected,
        "scope": "Public financial-domain smoke benchmark, not a customer-specific production benchmark",
        "predeclared_checks": {
            "document_recall_at_3_min_mean": 0.9,
            "answer_numeric_correctness_min_mean": 0.9,
            "unsupported_numeric_claims_allowed": 0,
        },
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(
        f"Prepared {len(selected)} public reports and separate reference answers in {destination}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    prepare(parser.parse_args().destination)
