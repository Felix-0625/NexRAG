"""Run scoring and HTTP load in dependency order; save stage evidence and acceptance records."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from evals.quality.report import publish
from evals.quality.run_native import dump
from evals.quality.runtime import ROOT
from scripts.native_processes import Supervisor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / ".local-run/quality-20261109")
    args = parser.parse_args()
    base = args.base.resolve()
    run = base / "native-recovered-01"
    output = base / "pipeline-01"
    output.mkdir(exist_ok=False)
    report = ROOT / "docs/acceptance/rag-quality-20261109.md"
    status = {
        "kind": "real-development-workflow",
        "formal_release_eligible": False,
        "stage": "waiting-full-corpus-and-real-generation",
        "stages": [],
    }
    dump(output / "status.json", status)
    deadline = time.monotonic() + 14400
    while not (base / "evals-native-baseline-01/run-ended.json").exists():
        if time.monotonic() > deadline or (run / "source-preservation.json").exists():
            status.update(
                stage="prerequisite_failed", reason="Native recovery or Evals did not become ready"
            )
            dump(output / "status.json", status)
            publish(base, report)
            raise RuntimeError(status["reason"])
        time.sleep(5)
    # Explicit, one-shot extension. It is consumed and retained by the owner.
    dump(
        run / "serve-policy.json",
        {
            "extend_seconds": 43200,
            "reason": "Complete actual C1/C5/C10 workloads and preserve every timeout",
        },
    )
    location = json.loads((run / "runtime-location.json").read_text("utf-8"))
    ollama = f"http://127.0.0.1:{location['ports']['ollama']}"
    core = ROOT / ".local-run/venvs/core-security/Scripts/python.exe"
    ragas = ROOT / ".local-run/acceptance/ragas-venv/Scripts/python.exe"
    dataset = base / "public-v4"
    stages = [
        (
            "ranking",
            core,
            "evals.quality.evaluate_retrieval",
            ["--dataset", dataset, "--run", run, "--output", base / "train-ranking-01"],
        ),
        (
            "external-judge-calibration",
            ragas,
            "evals.quality.calibrate_judge",
            [
                "--cases",
                base / "ragtruth-frozen-cases-v1.jsonl",
                "--output",
                base / "judge-calibration-01",
                "--ollama",
                ollama,
            ],
        ),
        (
            "real-generation-scores",
            ragas,
            "evals.quality.judge",
            [
                "--dataset",
                dataset,
                "--run",
                run,
                "--output",
                base / "train-generation-01",
                "--ollama",
                ollama,
            ],
        ),
        (
            "retrieval-http-load",
            core,
            "evals.quality.run_load",
            [
                "--run",
                run,
                "--dataset",
                dataset,
                "--hardware",
                base / "hardware.json",
                "--output",
                base / "retrieval-load-01",
                "--mode",
                "search",
            ],
        ),
        (
            "generation-http-load",
            core,
            "evals.quality.run_load",
            [
                "--run",
                run,
                "--dataset",
                dataset,
                "--hardware",
                base / "hardware.json",
                "--output",
                base / "chat-load-01",
                "--mode",
                "chat",
            ],
        ),
    ]
    supervisor = Supervisor(output, print)
    try:
        for name, python, module, arguments in stages:
            row = {"stage": name, "started_at": datetime.now(UTC).isoformat(), "state": "running"}
            status["stages"].append(row)
            status["stage"] = name
            dump(output / "status.json", status)
            publish(base, report)
            supervisor.start(
                name,
                [python, "-X", "utf8", "-m", module, *arguments],
                ROOT,
                dict(
                    os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"
                ),
            )
            child = supervisor.children[-1]
            while child.process.poll() is None:
                time.sleep(5)
            child.log.flush()
            row.update(
                exit_code=child.process.returncode,
                state="completed" if child.process.returncode == 0 else "failed",
                ended_at=datetime.now(UTC).isoformat(),
                log=str(output / "logs" / (name + ".log")),
            )
            dump(output / "status.json", status)
            publish(base, report)
            print(f"Actual workflow stage {name}: {row['state']}", flush=True)
        status["stage"] = "planned-development-stages-finished"
        dump(output / "status.json", status)
        publish(base, report)
        (run / "control.stop").write_text(
            "Owned benchmark workflow finished; no production service touched\n", "utf-8"
        )
    finally:
        supervisor.close()


if __name__ == "__main__":
    main()
