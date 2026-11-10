"""Calibrate LLMs on independent public human labels, outside this run's human agreement rate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from evals.quality.citations import validate_audit
from evals.quality.judge import AUDIT_PROMPT, Audit
from evals.quality.records import jsonl_line, read_jsonl


def freeze(provenance: Path) -> list[dict]:
    manifest = json.loads(provenance.read_text("utf-8"))
    files = {}
    for resource in manifest["resources"]:
        path = Path(resource["local_file"])
        if (
            not resource["verified_against_fixed_official_revision"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != resource["sha256"]
        ):
            raise ValueError("External human calibration provenance changed")
        files[path.name] = path
    sources = {
        r["source_id"]: r["source_info"]
        for r in read_jsonl(files["ragtruth-source_info.jsonl"])
        if r["task_type"] == "QA"
    }
    responses = [
        r
        for r in read_jsonl(files["ragtruth-response.jsonl"])
        if r["split"] == "test" and r["quality"] == "good" and r["source_id"] in sources
    ]
    responses.sort(
        key=lambda r: hashlib.sha256(("quality-external-judge-v1:" + r["id"]).encode()).hexdigest()
    )
    picked, used = [], set()
    for split in ("calibration", "reserved-audit"):
        for hallucinated in (False, True):
            accepted = 0
            for row in responses:
                if row["source_id"] in used or bool(row["labels"]) != hallucinated:
                    continue
                used.add(row["source_id"])
                picked.append(
                    {
                        "id": row["id"],
                        "source_id": row["source_id"],
                        "split": split,
                        "query": sources[row["source_id"]]["question"],
                        "source": sources[row["source_id"]]["passages"],
                        "answer": row["response"],
                        "human_hallucinated": hallucinated,
                    }
                )
                accepted += 1
                if accepted == 20:
                    break
            if accepted != 20:
                raise ValueError("Insufficient independent balanced public human labels")
    return picked


async def evaluate(args) -> None:
    parsed = urlsplit(args.ollama)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "External calibration uses an explicit credential-free local model service"
        )
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    import instructor
    from openai import AsyncOpenAI
    from ragas.llms.base import InstructorLLM

    rows = [r for r in read_jsonl(args.cases) if r["split"] == args.split]
    args.output.mkdir(parents=True, exist_ok=False)
    with httpx.Client(trust_env=False, timeout=30) as check:
        assert check.get(args.ollama + "/api/status").json()["cloud"]["disabled"] is True
        model = next(
            m
            for m in check.get(args.ollama + "/api/tags").json()["models"]
            if m["name"] == args.judge
        )
    journal = (args.output / "model-http.jsonl").open("w", encoding="utf-8")

    async def before(request):
        journal.write(jsonl_line({"phase": "request", "body": json.loads(request.content)}))
        journal.flush()

    async def after(response):
        journal.write(
            jsonl_line(
                {
                    "phase": "response",
                    "status": response.status_code,
                    "body": (await response.aread()).decode("utf-8", errors="replace"),
                }
            )
        )
        journal.flush()

    transport = httpx.AsyncClient(
        trust_env=False, timeout=180, event_hooks={"request": [before], "response": [after]}
    )
    client = AsyncOpenAI(
        base_url=args.ollama.rstrip("/") + "/v1",
        api_key="public-external-calibration",
        http_client=transport,
        max_retries=0,
    )
    llm = InstructorLLM(
        instructor.from_openai(client, mode=instructor.Mode.JSON),
        args.judge,
        "openai",
        temperature=0,
        top_p=1,
        max_tokens=4096,
        max_retries=1,
        seed=42,
    )
    (args.output / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "external-public-human-calibration",
                "formal_this_run_human_agreement": False,
                "model": model,
                "parameters": llm.model_args,
                "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
                "split": args.split,
                "audit_prompt_sha256": hashlib.sha256(AUDIT_PROMPT.encode()).hexdigest(),
                "planned": len(rows),
                "balanced": "20 positive/20 negative, source-disjoint; not an estimate of real prevalence",
            },
            indent=2,
        ),
        "utf-8",
    )
    results = []
    try:
        with (args.output / "rows.jsonl").open("w", encoding="utf-8") as stream:
            for index, row in enumerate(rows):
                result = {k: row[k] for k in ("id", "source_id", "human_hallucinated")}
                try:
                    prompt = AUDIT_PROMPT + json.dumps(
                        {
                            "question": row["query"],
                            "answer": row["answer"],
                            "current_passages": [
                                {"id": "KB1:0", "evidence_id": "KB1", "text": row["source"]}
                            ],
                        },
                        ensure_ascii=False,
                    )
                    audit = (await llm.agenerate(prompt, Audit)).model_dump()
                    # External source provenance is explicit. This marker is not
                    # an application ingestion receipt or a citation acceptance result.
                    passage = {
                        "id": "KB1:0",
                        "evidence_id": "KB1",
                        "source_id": row["source_id"],
                        "text": row["source"],
                        "authorized": True,
                        "ingest_version": "external-calibration-source",
                    }
                    verified = validate_audit(
                        audit, row["answer"], [passage], {row["source_id"]: row["source"]}
                    )
                    prediction = verified["hallucination_rate"]
                    result.update(
                        prediction=prediction,
                        agreement=(bool(prediction) == row["human_hallucinated"])
                        if prediction is not None
                        else False,
                        audit=verified,
                    )
                except Exception as exc:
                    result.update(
                        prediction=None,
                        agreement=False,
                        error=type(exc).__name__,
                        reason=str(exc)[:1000],
                    )
                results.append(result)
                stream.write(jsonl_line(result))
                stream.flush()
                summary = {
                    "kind": "external-public-human-calibration",
                    "planned": len(rows),
                    "completed": len(results),
                    "scored": sum(r["prediction"] is not None for r in results),
                    "agreement_over_all_planned": sum(r["agreement"] for r in results) / len(rows),
                    "formal_this_run_human_agreement": "unmeasured",
                    "errors_count_as_nonagreement": True,
                    "complete": len(results) == len(rows),
                }
                (args.output / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
                print(f"External human-label judge calibration {index + 1}/{len(rows)}", flush=True)
    finally:
        await client.close()
        journal.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ollama")
    parser.add_argument("--judge", default="hermes3:latest")
    parser.add_argument("--split", choices=("calibration", "reserved-audit"), default="calibration")
    args = parser.parse_args()
    if args.provenance:
        with args.cases.open("x", encoding="utf-8") as stream:
            for row in freeze(args.provenance):
                stream.write(jsonl_line(row))
        print("Frozen 80 external human-labelled cases; no judge outcomes inspected", flush=True)
    else:
        if not args.output or not args.ollama:
            parser.error("Scoring requires --output and --ollama")
        asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
