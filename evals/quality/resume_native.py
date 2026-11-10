"""Resume a stopped dev run: restore failed sources, run real HTTP; not fresh frozen acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx

from evals.quality.run_native import dump, inventory, query, request
from evals.quality.runtime import ROOT, NativeRuntime, archive_source_epoch, source_fingerprint


def restore_fixture_login(runtime: NativeRuntime) -> tuple[str, dict]:
    """Old random password was unsaved; reset its sole test account, keeping ACL and docs intact."""
    database = runtime.data / "data/openwebui/webui.db"
    password = secrets.token_urlsafe(32)
    with sqlite3.connect(database) as connection:
        users = connection.execute("SELECT id,email FROM user").fetchall()
        if len(users) != 1 or users[0][1] != "public-quality@example.test":
            raise ValueError("Recovery is restricted to the sole isolated public benchmark fixture")
        user_id = users[0][0]
        libraries = connection.execute("SELECT id,user_id,name FROM knowledge").fetchall()
        expected = {"Frozen public " + name for name in ("zh-general", "en-medical", "en-legal")}
        if {row[2] for row in libraries} != expected or any(row[1] != user_id for row in libraries):
            raise ValueError("Unexpected library ownership; recovery refused")
        with sqlite3.connect(runtime.run / "pre-recovery-webui.db") as backup:
            connection.backup(backup)
        hashed = (
            subprocess.run(
                [
                    str(ROOT / ".local-run/venvs/webui/Scripts/python.exe"),
                    "-c",
                    "import bcrypt,sys; print(bcrypt.hashpw(sys.stdin.buffer.read(),bcrypt.gensalt()).decode())",
                ],
                input=password.encode(),
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        changed = connection.execute(
            "UPDATE auth SET password=? WHERE id=? AND email=?", (hashed, user_id, users[0][1])
        ).rowcount
        if changed != 1:
            raise ValueError("Unexpected authentication fixture")
    dump(
        runtime.run / "fixture-recovery.json",
        {
            "kind": "development-fixture-credential-reset",
            "user_id": user_id,
            "reason": "Prior discovery coordinator did not persist its generated password",
            "acl_changed": False,
            "documents_changed": False,
            "formal_authentication_acceptance": False,
        },
    )
    return password, {row[2].removeprefix("Frozen public "): row[0] for row in libraries}


def recover(
    runtime: NativeRuntime,
    client: httpx.Client,
    previous: Path,
    dataset: Path,
    password: str,
    libraries: dict,
) -> tuple[dict, dict]:
    web = runtime.url("webui")
    account = request(
        client,
        "POST",
        web + "/api/v1/auths/signin",
        json={"email": "public-quality@example.test", "password": password},
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
    dump(runtime.run / "libraries.json", libraries)
    sources = json.loads((dataset / "sources.json").read_text("utf-8"))
    uploads = json.loads((previous / "uploads.json").read_text("utf-8"))
    dump(
        runtime.run / "inherited-upload-failures.json",
        json.loads((previous / "upload-failures.json").read_text("utf-8")),
    )
    with (runtime.run / "recovery-attempts.jsonl").open("w", encoding="utf-8") as stream:
        for index, source in enumerate(sources):
            row = {
                "source_id": source["id"],
                "sha256": source["sha256"],
                "cohort": source["cohort"],
            }
            started = time.perf_counter()
            try:
                data = (dataset / source["file"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != source["sha256"]:
                    raise ValueError("Frozen source bytes changed")
                inherited = uploads.get(source["id"])
                if inherited:
                    if inherited["sha256"] != source["sha256"] or not inherited["ok"]:
                        raise ValueError("Prior source receipt does not match frozen corpus")
                    file_id = inherited["file_id"]
                    row["operation"] = "verify-inherited-source"
                else:
                    row["operation"] = "retry-failed-original-upload"
                    response = client.post(
                        web + "/api/v1/files/",
                        timeout=360,
                        data={
                            "metadata": json.dumps({"knowledge_id": libraries[source["cohort"]]})
                        },
                        files={"file": (source["upload_name"], data, "text/plain")},
                    )
                    row["upload_status"] = response.status_code
                    response.raise_for_status()
                    file_id = response.json()["id"]
                row["file_id"] = file_id
                deadline = time.monotonic() + 360
                while time.monotonic() < deadline:
                    state = request(client, "GET", web + f"/api/v1/files/{file_id}/process/status")[
                        "status"
                    ]
                    if state == "failed":
                        raise RuntimeError("Production ingestion reported failure")
                    if state == "completed":
                        break
                    time.sleep(0.25)
                else:
                    raise TimeoutError("Original source processing did not complete")
                original = client.get(
                    web + f"/api/v1/files/{file_id}/content", params={"attachment": "true"}
                )
                original.raise_for_status()
                if hashlib.sha256(original.content).hexdigest() != source["sha256"]:
                    raise ValueError("Stored original differs from frozen public source")
                row.update(
                    ok=True, original_bytes_verified=True, seconds=time.perf_counter() - started
                )
                uploads[source["id"]] = row
            except (httpx.HTTPError, ValueError, RuntimeError, TimeoutError) as exc:
                row.update(
                    ok=False,
                    error=type(exc).__name__,
                    reason=str(exc),
                    seconds=time.perf_counter() - started,
                )
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            if not row["ok"]:
                dump(runtime.run / "recovery-failure.json", row)
                raise RuntimeError("Corpus recovery incomplete; queries were not run")
            if index == 0 or (index + 1) % 100 == 0 or row["operation"].startswith("retry"):
                print(
                    f"Full original-corpus recovery {index + 1}/{len(sources)}: {row['operation']}",
                    flush=True,
                )
    if set(uploads) != {s["id"] for s in sources}:
        raise ValueError("Recovered corpus source set differs from the frozen plan")
    dump(runtime.run / "uploads.json", uploads)
    acl = request(
        client,
        "POST",
        web + "/api/v1/nexrag/authorization/check",
        json={"user_id": account["id"]},
        headers={"Authorization": "Bearer " + runtime.base["NEXRAG_SHARED_API_KEY"]},
    )
    dump(runtime.run / "authorization.json", acl)
    return uploads, acl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cuda-packages", type=Path)
    parser.add_argument("--cuda-adapter", type=Path)
    parser.add_argument("--chat-limit", default=6, type=int)
    parser.add_argument("--serve-seconds", default=0, type=int)
    args = parser.parse_args()
    previous, dataset = args.previous.resolve(), args.dataset.resolve()
    runtime = NativeRuntime(
        args.output,
        resume_from=previous,
        cuda_packages=args.cuda_packages,
        cuda_adapter=args.cuda_adapter,
    )
    fingerprint = source_fingerprint()
    archive_source_epoch(runtime.run, fingerprint)
    dump(
        runtime.run / "run-manifest.json",
        {
            "stage": "recovered-development-baseline",
            "source": fingerprint,
            "dataset_manifest_sha256": hashlib.sha256(
                (dataset / "manifest.json").read_bytes()
            ).hexdigest(),
            "protocol_sha256": hashlib.sha256(
                Path(__file__).with_name("protocol-v1.json").read_bytes()
            ).hexdigest(),
            "effective_config_sha256": hashlib.sha256(
                (runtime.run / "effective-config.json").read_bytes()
            ).hexdigest(),
            "formal_release_eligible": False,
            "k": 10,
            "split": "train",
            "reason": "Preserves original upload failures and documents an explicit recovery/source epoch",
        },
    )
    try:
        password, libraries = restore_fixture_login(runtime)
        runtime.start()
        with httpx.Client(trust_env=False, timeout=120) as client:
            dump(
                runtime.run / "model-digests.json",
                request(client, "GET", runtime.url("ollama") + "/api/tags"),
            )
            uploads, acl = recover(runtime, client, previous, dataset, password, libraries)
            source_map = inventory(runtime, client, uploads, acl)
            dump(runtime.run / "source-map.json", source_map)
            query(runtime, client, dataset, acl, source_map, "train", args.chat_limit)
            dump(
                runtime.run / "ready-for-additional-development.json",
                {"ready": True, "holdout_used": False},
            )
            deadline = time.monotonic() + args.serve_seconds
            while not (runtime.run / "control.stop").exists():
                policy = runtime.run / "serve-policy.json"
                if policy.exists():
                    seconds = json.loads(policy.read_text("utf-8")).get("extend_seconds", 0)
                    if type(seconds) is not int or not 0 <= seconds <= 43200:
                        raise ValueError("Invalid isolated runtime serve extension")
                    deadline = max(deadline, time.monotonic() + seconds)
                    # Consume this explicit extension once; retaining the receipt
                    # avoids extending the deadline forever on every poll.
                    policy.rename(runtime.run / ("served-policy-" + str(time.time_ns()) + ".json"))
                if time.monotonic() >= deadline:
                    break
                runtime.supervisor.check()
                time.sleep(5)
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
