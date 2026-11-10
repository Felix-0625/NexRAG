"""Forward real search requests and score official labels only after responses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from evals.quality.records import read_jsonl
from evals.quality.spans import SpanScorer

VERIFIER = "nexrag-native-http-span-verifier-v1"


def main() -> None:
    """Run the command-line workflow with the supplied options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    token = os.environ["NEXRAG_QUALITY_EVALS_TOKEN"]
    credentials = json.loads((args.run / "session-credentials.json").read_text("utf-8"))
    location = json.loads((args.run / "runtime-location.json").read_text("utf-8"))
    acl = json.loads((args.run / "authorization.json").read_text("utf-8"))
    source_map = json.loads((args.run / "source-map.json").read_text("utf-8"))
    scorer = SpanScorer.from_files(args.dataset, args.run / "canonical-parents.json")
    cases = {case["id"]: case for case in read_jsonl(args.cases)}
    client = httpx.Client(trust_env=False, timeout=120)

    Handler = _make_handler(
        args, token, credentials, location, acl, source_map, scorer, cases, client
    )

    try:
        HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    finally:
        client.close()


def _make_handler(args, token, credentials, location, acl, source_map, scorer, cases, client):
    def execute(payload: dict) -> dict:
        """Handle execute for this workflow."""
        case = cases[payload["metadata"]["case_id"]]
        if payload["application_version"] != args.version or payload["config"] != {"k": 10}:
            raise ValueError("Execution differs from frozen application/configuration")
        if payload["input"] != case["query"]:
            raise ValueError("Query differs from frozen official question")
        started = time.perf_counter()
        status = None
        http_error = None
        try:
            # No metadata, references, qrels or expected answers enter the application.
            response = client.post(
                f"http://127.0.0.1:{location['ports']['retrieval']}/search",
                headers={"Authorization": "Bearer " + credentials["retrieval_token"]},
                json={
                    "queries": [case["query"]],
                    "collection_names": acl["knowledge_scopes"],
                    "k": 10,
                },
            )
            status = response.status_code
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            http_error = {"type": type(exc).__name__, "response_excerpt": exc.response.text[:500]}
        except httpx.RequestError as exc:
            http_error = {"type": type(exc).__name__, "detail": str(exc)[:500]}
        elapsed = time.perf_counter() - started
        if http_error:
            # A failed native call still has a known zero local API charge.
            # Omit unobserved facts so Evals keeps them unverifiable and continues.
            body, sources = None, []
            scored = {
                "id": case["id"],
                "split": case["split"],
                "cohort": case["cohort"],
                "source_group": case["group"],
                "scores": {},
                "errors": {"http": http_error["type"]},
            }
            facts = {"retrieval": {}, "security": {}}
        else:
            body = response.json()
            sources = [source_map.get(meta.get("file_id")) for meta in body["metadatas"][0]]
            authorized = all(
                meta.get("collection_name") in acl["knowledge_scopes"] and source is not None
                for meta, source in zip(body["metadatas"][0], sources, strict=True)
            )
            scored = scorer.score(case, body, sources)
            values = scored["scores"]
            passed = (
                values["recall_at_10"] > 0.90
                and values["mrr"] > 0.90
                and values["ndcg_at_10"] > 0.85
                if not scored["errors"]
                else None
            )
            facts = {
                "retrieval": {"passed": passed, **values},
                "security": {"authorized": authorized},
            }
        return _receipt_observation(args, scored, elapsed, status, http_error, body, sources, facts)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            """Handle reply for this workflow."""
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            """Handle do GET for this workflow."""
            self.reply(200, {"version": args.version, "kind": "real-native-http-forwarder"})

        def do_POST(self):
            """Handle do POST for this workflow."""
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.reply(401, {"error": "Unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100_000:
                    raise ValueError("Invalid Evals request size")
                self.reply(200, execute(json.loads(self.rfile.read(length))))
            except Exception as exc:
                self.reply(500, {"error": type(exc).__name__, "detail": str(exc)[:500]})

        def log_message(self, *_):
            """Handle log message for this workflow."""
            pass

    return Handler


def _receipt_observation(args, scored, elapsed, status, http_error, body, sources, facts):
    receipt = {
        **scored,
        "receipt_kind": "real-native-http",
        "seconds": elapsed,
        "status": status,
        "http_error": http_error,
        "response": body,
        "source_ids": sources,
        "application_version": args.version,
        "facts": facts,
        "boundary": "Actual native HTTP retrieval; no query, embedding, pair-score or result replay cache",
    }
    data = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
    path = args.output / (hashlib.sha256(data).hexdigest() + ".json")
    path.write_bytes(data)
    evidence = {
        name: {
            "source": VERIFIER,
            "locator": str(path),
            "version": args.version,
            "scope": "local",
            "collected_at": datetime.now(UTC).isoformat(),
        }
        for name in ("retrieval.passed", "security.authorized")
    }
    return {
        "output": "\n".join(body["documents"][0]) if body is not None else "Retrieval unavailable",
        "facts": facts,
        "evidence": evidence,
        "application_version": args.version,
        "cost": 0,
        "cost_source": "local-model-no-paid-provider",
        "pricing_version": "local-api-charges-only-v1",
        "latency": elapsed,
    }


if __name__ == "__main__":
    main()
