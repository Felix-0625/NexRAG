"""Upload frozen sources to an empty knowledge base, then run real HTTP retrieval and generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import time
from pathlib import Path

import httpx

from evals.quality.records import read_jsonl
from evals.quality.runtime import NativeRuntime, archive_source_epoch, source_fingerprint


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def request(client: httpx.Client, method: str, url: str, **kwargs):
    response = client.request(method, url, **kwargs)
    if response.is_error:
        raise RuntimeError(f"{method} {response.url.path}: HTTP {response.status_code}")
    return response.json()


def ingest(runtime: NativeRuntime, client: httpx.Client, dataset: Path) -> tuple[dict, dict]:
    web = runtime.url("webui")
    account = request(
        client,
        "POST",
        web + "/api/v1/auths/signup",
        json={
            "name": "Public quality benchmark",
            "email": "public-quality@example.test",
            "password": secrets.token_urlsafe(32),
        },
    )
    client.headers["Authorization"] = "Bearer " + account["token"]
    dump(
        runtime.run / "session-credentials.json",
        {
            "webui_token": account["token"],
            "retrieval_token": runtime.base["RETRIEVAL_API_KEY"],
            "user_id": account["id"],
        },
    )
    sources = json.loads((dataset / "sources.json").read_text("utf-8"))
    libraries = {}
    for cohort in sorted({s["cohort"] for s in sources}):
        libraries[cohort] = request(
            client,
            "POST",
            web + "/api/v1/knowledge/create",
            json={
                "name": "Frozen public " + cohort,
                "description": "Entire published source corpus; no answer annotations",
            },
        )["id"]
    dump(runtime.run / "libraries.json", libraries)
    uploads = {}
    failures = []
    with (runtime.run / "upload-attempts.jsonl").open("w", encoding="utf-8") as log:
        for index, source in enumerate(sources):
            started = time.perf_counter()
            record = {
                "source_id": source["id"],
                "cohort": source["cohort"],
                "sha256": source["sha256"],
            }
            try:
                data = (dataset / source["file"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != source["sha256"]:
                    raise ValueError("Frozen original source bytes changed")
                file_id = request(
                    client,
                    "POST",
                    web + "/api/v1/files/",
                    data={"metadata": json.dumps({"knowledge_id": libraries[source["cohort"]]})},
                    files={"file": (source["upload_name"], data, "text/plain")},
                )["id"]
                record["file_id"] = file_id
                deadline = time.monotonic() + 305
                while time.monotonic() < deadline:
                    status = request(
                        client, "GET", web + f"/api/v1/files/{file_id}/process/status"
                    )["status"]
                    if status == "failed":
                        raise RuntimeError("Production ingestion reported failure")
                    if status == "completed":
                        visible = request(
                            client,
                            "GET",
                            web + f"/api/v1/knowledge/{libraries[source['cohort']]}/files",
                        )["items"]
                        if any(row["id"] == file_id for row in visible):
                            break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("Uploaded source did not become ready and linked")
                original = client.get(
                    web + f"/api/v1/files/{file_id}/content", params={"attachment": "true"}
                )
                original.raise_for_status()
                if hashlib.sha256(original.content).hexdigest() != source["sha256"]:
                    raise ValueError("Stored original bytes differ from frozen public source")
                record.update(
                    ok=True,
                    ready_seconds=time.perf_counter() - started,
                    original_bytes_verified=True,
                )
                uploads[source["id"]] = record
            except (httpx.HTTPError, ValueError, RuntimeError, TimeoutError) as exc:
                record.update(
                    ok=False,
                    seconds=time.perf_counter() - started,
                    error=type(exc).__name__,
                    reason=str(exc),
                )
                failures.append(record)
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            if index == 0 or (index + 1) % 20 == 0 or not record["ok"]:
                print(
                    f"Original-source upload {index + 1}/{len(sources)}, failed={len(failures)}",
                    flush=True,
                )
                dump(runtime.run / "uploads.json", uploads)
                dump(runtime.run / "upload-failures.json", failures)
    dump(runtime.run / "uploads.json", uploads)
    dump(runtime.run / "upload-failures.json", failures)
    dump(runtime.run / "libraries.json", libraries)
    # An incomplete corpus is not an easier benchmark. Do not proceed to quality
    # scoring or query only the successfully ingested documents.
    if failures:
        raise RuntimeError(
            f"Corpus incomplete: {len(failures)} original-source uploads failed; receipts retained"
        )
    acl = request(
        client,
        "POST",
        web + "/api/v1/nexrag/authorization/check",
        json={"user_id": account["id"]},
        headers={"Authorization": "Bearer " + runtime.base["NEXRAG_SHARED_API_KEY"]},
    )
    dump(runtime.run / "authorization.json", {k: v for k, v in acl.items() if k not in {"token"}})
    dump(
        runtime.run / "session-credentials.json",
        {
            "webui_token": account["token"],
            "retrieval_token": runtime.base["RETRIEVAL_API_KEY"],
            "user_id": account["id"],
        },
    )
    return uploads, acl


def inventory(runtime: NativeRuntime, client: httpx.Client, uploads: dict, acl: dict) -> dict:
    scopes = acl["knowledge_scopes"]
    source_map = {
        hashlib.sha256(f"{record['file_id']}:{scope}".encode()).hexdigest(): source_id
        for source_id, record in uploads.items()
        for scope in scopes
    }
    offset = None
    parents = {}
    chunks = 0
    url = runtime.url("qdrant") + f"/collections/{runtime.base['QDRANT_INDEX']}/points/scroll"
    with (runtime.run / "native-children.jsonl").open("w", encoding="utf-8") as stream:
        while True:
            result = request(
                client,
                "POST",
                url,
                json={
                    "limit": 128,
                    "offset": offset,
                    "with_payload": True,
                    "with_vector": False,
                    "filter": {"must": [{"key": "meta.collection_name", "match": {"any": scopes}}]},
                },
            )["result"]
            for point in result["points"]:
                meta = point["payload"]["meta"]
                if meta["collection_name"] not in scopes or meta["file_id"] not in source_map:
                    raise ValueError(
                        "Native inventory contains a source outside the authorized public corpus"
                    )
                source_id = source_map[meta["file_id"]]
                text = (
                    meta.get("parent_text")
                    or meta.get("parent_content")
                    or point["payload"]["content"]
                )
                key = hashlib.sha256(
                    json.dumps([source_id, text], ensure_ascii=False).encode()
                ).hexdigest()
                parents[key] = {"id": key, "source_id": source_id, "text": text}
                stream.write(
                    json.dumps({"source_id": source_id, "point": point}, ensure_ascii=False) + "\n"
                )
                chunks += 1
            stream.flush()
            offset = result["next_page_offset"]
            if offset is None:
                break
    if {p["source_id"] for p in parents.values()} != set(uploads):
        raise ValueError("Uploaded public sources missing from native parent catalogue")
    dump(runtime.run / "canonical-parents.json", list(parents.values()))
    dump(
        runtime.run / "inventory-summary.json",
        {"sources": len(uploads), "children": chunks, "parents": len(parents)},
    )
    return source_map


def query(
    runtime: NativeRuntime,
    client: httpx.Client,
    dataset: Path,
    acl: dict,
    source_map: dict,
    split: str,
    chat_limit: int,
) -> None:
    cases = read_jsonl(dataset / (split + ".jsonl"))
    with (runtime.run / f"{split}-retrieval.jsonl").open("w", encoding="utf-8") as stream:
        for index, case in enumerate(cases):
            started = time.perf_counter()
            row = {"id": case["id"], "cohort": case["cohort"], "split": split}
            try:
                response = client.post(
                    runtime.url("retrieval") + "/search",
                    headers={"Authorization": "Bearer " + runtime.base["RETRIEVAL_API_KEY"]},
                    json={
                        "queries": [case["query"]],
                        "collection_names": acl["knowledge_scopes"],
                        "k": 10,
                    },
                )
                row.update(status=response.status_code, seconds=time.perf_counter() - started)
                response.raise_for_status()
                body = response.json()
                row["response"] = body
                row["source_ids"] = [
                    source_map.get(meta.get("file_id")) for meta in body["metadatas"][0]
                ]
                row["authorized"] = all(
                    m.get("collection_name") in acl["knowledge_scopes"]
                    for m in body["metadatas"][0]
                )
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                row.update(error=type(exc).__name__, seconds=time.perf_counter() - started)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            if index == 0 or (index + 1) % 20 == 0:
                print(f"Real HTTP {split} retrieval {index + 1}/{len(cases)}", flush=True)
    selected = []
    for cohort in sorted({c["cohort"] for c in cases}):
        selected.extend([c for c in cases if c["cohort"] == cohort][:chat_limit])
    query_chat(runtime, client, split, selected)


def query_chat(
    runtime: NativeRuntime,
    client: httpx.Client,
    split: str,
    selected: list[dict],
    *,
    expected_model: dict | None = None,
) -> None:
    models = request(client, "GET", runtime.url("webui") + "/api/models")["data"]
    choices = [m for m in models if m["id"].startswith("nexrag-model-")]
    model = next((m for m in choices if m.get("is_default")), choices[0])
    identity = {"id": model["id"], "name": model.get("name")}
    if expected_model is not None and identity != expected_model:
        raise ValueError("Real chat endpoint selected a different model")
    dump(runtime.run / "chat-model.json", identity)
    with (runtime.run / f"{split}-chat.jsonl").open("w", encoding="utf-8") as stream:
        for index, case in enumerate(selected):
            request_id = hashlib.sha256((split + ":" + case["id"]).encode()).hexdigest()
            started = time.perf_counter()
            row = {
                "id": case["id"],
                "cohort": case["cohort"],
                "request_id": request_id,
                "split": split,
            }
            try:
                response = client.post(
                    runtime.url("webui") + "/api/chat/completions",
                    timeout=125,
                    headers={"X-OpenWebUI-Message-Id": request_id},
                    json={
                        "model": model["id"],
                        "stream": False,
                        "messages": [{"role": "user", "content": case["query"]}],
                    },
                )
                row.update(
                    status=response.status_code,
                    response=response.json(),
                    seconds=time.perf_counter() - started,
                )
            except (httpx.HTTPError, ValueError) as exc:
                row.update(error=type(exc).__name__, seconds=time.perf_counter() - started)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(
                f"Real HTTP {split} chat {index + 1}/{len(selected)}: {row.get('status', row.get('error'))}, {row['seconds']:.2f}s",
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hardware", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cuda-packages", type=Path)
    parser.add_argument("--cuda-adapter", type=Path)
    parser.add_argument("--split", choices=("train", "selection", "holdout"), default="train")
    parser.add_argument("--chat-limit", type=int, default=6)
    args = parser.parse_args()
    dataset = args.dataset.resolve()
    config = json.loads(args.config.read_text("utf-8")) if args.config else None
    runtime = NativeRuntime(
        args.output, config=config, cuda_packages=args.cuda_packages, cuda_adapter=args.cuda_adapter
    )
    fingerprint = source_fingerprint()
    archive_source_epoch(runtime.run, fingerprint)
    manifest = json.loads((dataset / "manifest.json").read_text("utf-8"))
    dump(
        runtime.run / "run-manifest.json",
        {
            "dataset_manifest_sha256": hashlib.sha256(
                (dataset / "manifest.json").read_bytes()
            ).hexdigest(),
            "dataset": manifest,
            "source": fingerprint,
            "hardware": json.loads(args.hardware.read_text("utf-8-sig")),
            "protocol_sha256": hashlib.sha256(
                (Path(__file__).with_name("protocol-v1.json")).read_bytes()
            ).hexdigest(),
            "k": 10,
            "split": args.split,
            "stage": "fresh-native-baseline",
            "formal_release_eligible": False,
            "reason": "Baseline discovery; final holdout, full load and human review remain required",
            "config": {
                k: v
                for k, v in runtime.base.items()
                if k.startswith(("CHUNK_", "PARENT_", "RERANK_", "HYBRID_"))
                or k in {"KNOWLEDGE_TOP_K", "RETRIEVAL_CONTEXT_MODE", "RETRIEVAL_CANDIDATE_LIMIT"}
            },
            "cuda_adapter_sha256": hashlib.sha256(args.cuda_adapter.read_bytes()).hexdigest()
            if args.cuda_adapter
            else None,
        },
    )
    try:
        runtime.start()
        with httpx.Client(trust_env=False, timeout=120) as client:
            models = request(client, "GET", runtime.url("ollama") + "/api/tags")
            dump(runtime.run / "model-digests.json", models)
            uploads, acl = ingest(runtime, client, dataset)
            source_map = inventory(runtime, client, uploads, acl)
            query(runtime, client, dataset, acl, source_map, args.split, args.chat_limit)
    finally:
        runtime.close()
        dump(
            runtime.run / "source-preservation.json",
            {
                "source_unchanged_during_run": fingerprint == source_fingerprint(),
                "owned_services_stopped": True,
                "original_production_data_opened": False,
            },
        )


if __name__ == "__main__":
    main()
