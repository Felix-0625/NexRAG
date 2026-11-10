"""Build fixed loads from real sessions; run isolated 1/5/10 concurrency groups per endpoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from evals.quality.http_load import exercise
from evals.quality.records import jsonl_line, read_jsonl
from evals.quality.run_native import dump


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--hardware", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("search", "chat"), required=True)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    credentials = json.loads((args.run / "session-credentials.json").read_text("utf-8"))
    acl = json.loads((args.run / "authorization.json").read_text("utf-8"))
    location = json.loads((args.run / "runtime-location.json").read_text("utf-8"))
    cases = read_jsonl(args.dataset / "train.jsonl")
    groups = {
        cohort: [c for c in cases if c["cohort"] == cohort]
        for cohort in ("zh-general", "en-medical", "en-legal")
    }
    if len({len(rows) for rows in groups.values()}) != 1:
        raise ValueError("Frozen load sequence requires balanced public development cohorts")
    ordered = [rows[i] for i in range(len(next(iter(groups.values())))) for rows in groups.values()]
    model = (
        json.loads((args.run / "chat-model.json").read_text("utf-8"))["id"]
        if args.mode == "chat"
        else None
    )
    inputs = args.output / "requests.jsonl"
    with inputs.open("x", encoding="utf-8") as stream:
        for case in ordered:
            body = (
                {"queries": [case["query"]], "collection_names": acl["knowledge_scopes"], "k": 10}
                if args.mode == "search"
                else {
                    "model": model,
                    "stream": False,
                    "messages": [{"role": "user", "content": case["query"]}],
                }
            )
            stream.write(
                jsonl_line({"case_id": case["id"], "cohort": case["cohort"], "body": body})
            )
    dump(
        args.output / "source-epoch.json",
        {
            "application_run": str(args.run.resolve()),
            "run_manifest_sha256": hashlib.sha256(
                (args.run / "run-manifest.json").read_bytes()
            ).hexdigest(),
            "loaded_source_archive_sha256": hashlib.sha256(
                (args.run / "loaded-source-epoch.zip").read_bytes()
            ).hexdigest(),
            "models": json.loads((args.run / "model-digests.json").read_text("utf-8")),
            "development_corpus_continuation": True,
            "formal_release_eligible": False,
            "replay_or_query_cache": False,
            "cold_model_claim": False,
            "cache_disclosure": "Native model/index weights and prompt-prefix warmup allowed; no query/embedding/pair-score/result replay",
            "boundary": "Real production HTTP requests. Full application correctness is a separate quality gate.",
        },
    )
    token = credentials["retrieval_token" if args.mode == "search" else "webui_token"]
    os.environ["NEXRAG_QUALITY_LOAD_TOKEN"] = token
    port = location["ports"]["retrieval" if args.mode == "search" else "webui"]
    path = "/search" if args.mode == "search" else "/api/chat/completions"
    workload = argparse.Namespace(
        url=f"http://127.0.0.1:{port}{path}",
        mode=args.mode,
        requests=inputs,
        hardware=args.hardware,
        output=args.output / "http",
        token_env="NEXRAG_QUALITY_LOAD_TOKEN",
        headers_env=None,
        count=args.count,
        timeout=args.timeout,
        capture_body=True,
    )
    try:
        dump(args.output / "summary.json", asyncio.run(exercise(workload)))
    finally:
        os.environ.pop("NEXRAG_QUALITY_LOAD_TOKEN", None)


if __name__ == "__main__":
    main()
