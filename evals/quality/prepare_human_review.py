"""Prepare a blinded local review packet from actual NexRAG HTTP receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evals.quality.generation_inputs import load_inputs
from evals.quality.records import jsonl_line, read_jsonl

RUBRIC = """# NexRAG 人工盲审

每位评审者独立查看 review-items.jsonl，在单独副本中填写 review-template.jsonl。仅依据问题、
实际回答和本轮可见的授权证据判断。题包不包含 LLM Judge 输出、模型名称或参考答案。

- 标出回答中每条可核验事实。逐条记下回答原句、支持它的证据 ID 和准确引句。
- 若任何事实与证据矛盾、缺少支持或使用伪造引用，hallucinated 填 true。所有可核验事实
  均被完整支持时填 false。没有实质回答或无法裁决时填 null，并说明原因。
- grounded 填写完整结论是否由本轮证据支持。citation_accurate 填写每条事实是否紧邻
  当前有效的知识证据 ID，且相应证据足以支持含限定条件的完整事实。
- 无法判断的条目保留 null 和原因。各评审者提交前独立填写，不看其他评审者的结果。
"""


def prepare(dataset: Path, run: Path, split: str, output: Path) -> dict:
    plan = dataset / f"{split}.jsonl"
    chat = run / f"{split}-chat.jsonl"
    plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
    chat_hash = hashlib.sha256(chat.read_bytes()).hexdigest()
    cases = read_jsonl(plan)
    observations = {}
    for row in load_inputs(dataset, run, split):
        if row["id"] in observations:
            raise ValueError("Duplicate real HTTP answer receipt")
        observations[row["id"]] = row
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate frozen case ID")
    items, index, unavailable = [], [], []
    for case in cases:
        case_id = case["id"]
        row = observations.get(case_id)
        reason = None
        if row is None:
            reason = "missing-http-receipt"
        elif row["errors"]:
            reason = row["errors"]
        elif row["model_calls"] < 1:
            reason = "missing-real-model-call"
        elif any(not passage["authorized"] for passage in row["passages"]):
            reason = "unauthorized-context"
        if reason is not None:
            unavailable.append({"case_id": case_id, "cohort": case["cohort"], "reason": reason})
            continue
        assert row is not None
        blind_id = hashlib.sha256(f"{plan_hash}:{chat_hash}:{case_id}".encode()).hexdigest()
        passages = [
            {
                key: passage[key]
                for key in ("id", "evidence_id", "source_id", "ingest_version", "text")
            }
            for passage in row["passages"]
        ]
        items.append(
            {
                "review_id": blind_id,
                "question": row["query"],
                "answer": row["answer"],
                "current_passages": passages,
            }
        )
        index.append(
            {
                "review_id": blind_id,
                "case_id": case_id,
                "cohort": case["cohort"],
                "source_group": case["group"],
                "trace_sha256": row["trace_sha256"],
            }
        )

    items.sort(key=lambda item: item["review_id"])
    output.mkdir(parents=True, exist_ok=False)
    packet = output / "review-items.jsonl"
    packet.write_text("".join(map(jsonl_line, items)), encoding="utf-8")
    template = (
        {
            "review_id": item["review_id"],
            "hallucinated": None,
            "grounded": None,
            "citation_accurate": None,
            "claims": [],
            "reason": "",
        }
        for item in items
    )
    (output / "review-template.jsonl").write_text("".join(map(jsonl_line, template)), "utf-8")
    (output / "instructions.md").write_text(RUBRIC, "utf-8")
    summary = {
        "kind": "unrated-blinded-review-packet",
        "split": split,
        "planned": len(cases),
        "real_http_receipts": len(observations),
        "ready_for_review": len(items),
        "unavailable": len(unavailable),
        "independent_human_reviews": 0,
        "formal_human_judge_agreement": "unmeasured",
    }
    (output / "coordinator-index.json").write_text(
        json.dumps(
            {
                **summary,
                "case_plan_sha256": plan_hash,
                "real_http_sha256": chat_hash,
                "review_items_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
                "index": index,
                "unavailable_cases": unavailable,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "selection", "holdout"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.dataset, args.run, args.split, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
