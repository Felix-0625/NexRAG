"""Read real HTTP answers and model-visible tool messages; never inject score labels as evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evals.quality.records import read_jsonl


def load_inputs(dataset: Path, run: Path, split: str) -> list[dict]:
    cases = {row["id"]: row for row in read_jsonl(dataset / (split + ".jsonl"))}
    sources = json.loads((dataset / "sources.json").read_text("utf-8"))
    originals = {}
    for source in sources:
        data = (dataset / source["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError("Frozen original bytes changed")
        originals[source["id"]] = data.decode("utf-8")
    acl = json.loads((run / "authorization.json").read_text("utf-8"))
    uploads = json.loads((run / "uploads.json").read_text("utf-8"))
    source_map = {
        hashlib.sha256(f"{row['file_id']}:{scope}".encode()).hexdigest(): source_id
        for source_id, row in uploads.items()
        for scope in acl["knowledge_scopes"]
    }
    records = []
    for row in read_jsonl(run / (split + "-chat.jsonl")):
        case = cases[row["id"]]
        item = {
            "id": case["id"],
            "cohort": case["cohort"],
            "source_group": case["group"],
            "split": split,
            "query": case["query"],
            "reference": case["reference"],
            "receipt_kind": "real-native-http",
            "http_receipt": row,
            "passages": [],
            "errors": {},
        }
        try:
            if row.get("status") != 200:
                raise ValueError("Production chat HTTP failed")
            body = row["response"]
            item["answer"] = body["choices"][0]["message"]["content"]
            if not item["answer"] or body["choices"][0].get("finish_reason") != "stop":
                raise ValueError("Incomplete production chat answer")
            key = hashlib.sha256(body.get("id", row["request_id"]).encode()).hexdigest()
            trace = run / "model-contexts" / (key + ".json")
            if not trace.exists():
                key = hashlib.sha256(row["request_id"].encode()).hexdigest()
                trace = run / "model-contexts" / (key + ".json")
            captured = json.loads(trace.read_text("utf-8"))
            if captured.get("trace_id") not in {body.get("id"), row["request_id"]}:
                raise ValueError("Captured tool evidence belongs to another turn")
            item["trace_sha256"] = hashlib.sha256(trace.read_bytes()).hexdigest()
            item["trace_status"] = captured["status"]
            item["model_calls"] = captured["model_calls"]
            item["rule_scores"] = captured["rule_scores"]
            item["answer_origin"] = (
                "application-grounding-fallback"
                if captured["rule_scores"].get("grounding_fallback")
                else "model"
            )
            for call in captured["tool_returns"]:
                content = call["content"]
                if not isinstance(content, dict):
                    raise ValueError(
                        "Model-visible knowledge tool message is not a structured receipt"
                    )
                evidence_id = content.get("evidence_id")
                for index, document in enumerate(content.get("documents", [])):
                    metadata = document.get("metadata", {})
                    source_id = source_map.get(metadata.get("file_id"))
                    if not evidence_id or not source_id:
                        raise ValueError(
                            "Model-visible knowledge source cannot be mapped to this corpus"
                        )
                    item["passages"].append(
                        {
                            "id": f"{evidence_id}:{index}",
                            "evidence_id": evidence_id,
                            "source_id": source_id,
                            "text": document["content"],
                            "metadata": metadata,
                            "ingest_version": metadata.get("ingest_version"),
                            "authorized": metadata.get("collection_name")
                            in acl["knowledge_scopes"],
                        }
                    )
            item["originals"] = {
                p["source_id"]: originals[p["source_id"]] for p in item["passages"]
            }
        except (KeyError, IndexError, TypeError, ValueError, FileNotFoundError) as exc:
            item["errors"]["generation_input"] = {"kind": type(exc).__name__, "reason": str(exc)}
        records.append(item)
    return records
