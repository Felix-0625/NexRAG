"""Measure loopback HTTP load while retaining warmup and failure receipts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from evals.quality.metrics import http_summary
from evals.quality.records import read_jsonl

PROTOCOL = Path(__file__).with_name("protocol-v1.json")


def dump(path: Path, value: object) -> None:
    """Write the supplied value as UTF-8 JSON."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def completed_response(response: httpx.Response, mode: str) -> bool:
    """Check response completeness without claiming business correctness."""
    if response.status_code != 200:
        return False
    try:
        body = response.json()
        if mode == "search":
            return len(body["documents"]) == len(body["metadatas"]) == len(
                body["distances"]
            ) == 1 and len(body["documents"][0]) == len(body["metadatas"][0]) == len(
                body["distances"][0]
            )
        choice = body["choices"][0]
        return bool(choice["message"]["content"]) and choice.get("finish_reason") == "stop"
    except (KeyError, IndexError, TypeError, ValueError):
        return False


async def exercise(args: argparse.Namespace) -> dict:
    """Exercise the configured service and retain every load-test receipt."""
    samples, headers = _prepare_load(args)
    results = {}
    timeout = httpx.Timeout(args.timeout)
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=10)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, trust_env=False, limits=limits
    ) as client:
        attempt = _make_attempt(args, samples, client)

        cold = await attempt(0, "first_observation", 1)
        dump(
            args.output / "first-request.json",
            dict(
                cold,
                cold_status="Requires service-start receipt; first harness request alone does not prove cold model",
            ),
        )
        warm = [await attempt(i, "warmup", 1) for i in range(20)]
        dump(args.output / "warmup.json", warm)
        if not all(row["ok"] for row in warm):
            dump(
                args.output / "incomplete.json",
                {"reason": "Warmup requests failed; no measured group was silently skipped"},
            )
            raise RuntimeError("Warmup failed; inspect retained receipts")
        for concurrency in (1, 5, 10):
            queue: asyncio.Queue[int] = asyncio.Queue()
            for i in range(args.count):
                queue.put_nowait(i)
            rows = []
            start = time.perf_counter()
            with (args.output / f"requests-c{concurrency}.jsonl").open(
                "w", encoding="utf-8"
            ) as stream:

                async def worker() -> None:
                    """Handle worker for this workflow."""
                    while not queue.empty():
                        index = queue.get_nowait()
                        row = await attempt(index, "measured", concurrency)
                        rows.append(row)
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        stream.flush()
                        queue.task_done()
                        if len(rows) % 100 == 0:
                            print(
                                f"{args.mode} concurrency={concurrency} {len(rows)}/{args.count}",
                                flush=True,
                            )

                await asyncio.gather(*(worker() for _ in range(concurrency)))
            results[str(concurrency)] = http_summary(
                rows, expected=args.count, wall_seconds=time.perf_counter() - start
            )
            dump(args.output / "summary.json", results)
    return results


def main() -> None:
    """Run the command-line workflow with the supplied options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--mode", choices=("search", "chat"), required=True)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--hardware", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--headers-env")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--capture-body", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(exercise(args))), flush=True)


def _make_attempt(args, samples, client):
    async def attempt(index: int, phase: str, concurrency: int) -> dict:
        """Handle attempt for this workflow."""
        sample = samples[index % len(samples)]
        request_id = f"{phase}-{concurrency}-{index}"
        start = time.perf_counter()
        row = {
            "request_id": request_id,
            "case_id": sample["case_id"],
            "phase": phase,
            "concurrency": concurrency,
            "cohort": sample.get("cohort"),
        }
        try:
            response = await client.post(
                args.url,
                json=sample["body"],
                headers={"X-OpenWebUI-Message-Id": hashlib.sha256(request_id.encode()).hexdigest()},
            )
            row.update(
                status=response.status_code,
                ok=completed_response(response, args.mode),
                response_sha256=hashlib.sha256(response.content).hexdigest(),
                response_bytes=len(response.content),
            )
            if args.capture_body:
                row["response"] = response.text
        except httpx.HTTPError as exc:
            row.update(ok=False, status=None, error=type(exc).__name__)
        row["seconds"] = time.perf_counter() - start
        return row

    return attempt


def _prepare_load(args):
    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This frozen protocol targets this machine's loopback HTTP service")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "Do not include credentials, query parameters or fragments in service URLs"
        )
    samples = read_jsonl(args.requests)
    if not samples or any(
        not isinstance(s.get("body"), dict) or not s.get("case_id") for s in samples
    ):
        raise ValueError("Requests require case_id and a production HTTP request body")
    for sample in samples:
        body = sample["body"]
        if args.mode == "search" and (body.get("k") != 10 or len(body.get("queries", [])) != 1):
            raise ValueError("Formal retrieval load uses one real query per request, K=10")
        if args.mode == "chat" and body.get("stream") is not False:
            raise ValueError("Frozen full-response chat load requires stream=false")
    if args.count < 1000:
        raise ValueError("Formal load requires at least 1,000 requests per concurrency group")
    args.output.mkdir(parents=True, exist_ok=False)
    header_value = os.environ[args.token_env]
    headers = {"Authorization": "Bearer " + header_value}
    if args.headers_env:
        extra = json.loads(os.environ[args.headers_env])
        if not isinstance(extra, dict) or any(not isinstance(v, str) for v in extra.values()):
            raise ValueError("Additional headers must be a JSON object of string values")
        headers.update(extra)
    frozen = {
        "protocol_id": "nexrag-quality-v1-20261109",
        "protocol_sha256": hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
        "input_sha256": hashlib.sha256(args.requests.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "hardware_manifest_sha256": hashlib.sha256(args.hardware.read_bytes()).hexdigest(),
        "hardware": json.loads(args.hardware.read_text("utf-8-sig")),
        "runtime": {"platform": platform.platform(), "python": platform.python_version()},
        "service_url": args.url,
        "kind": "actual_http_load",
        "mode": args.mode,
        "concurrency": [1, 5, 10],
        "requests_per_group": args.count,
        "independent_cases": len({s["case_id"] for s in samples}),
        "sequence": "input order repeated modulo sample count; identical across concurrency groups",
        "cache_policy": "caller must attest absence of answer/embedding/pair-score caches in run manifest",
        "started_at": datetime.now(UTC).isoformat(),
        "timeout_seconds": args.timeout,
        "warmup_count": 20,
    }
    dump(args.output / "manifest.json", frozen)
    return samples, headers


if __name__ == "__main__":
    main()
