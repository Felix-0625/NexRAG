"""Run real HTTP development baselines while preserving the holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.quality.http_gateway import VERIFIER
from evals.quality.records import read_jsonl
from evals.quality.run_native import dump
from evals.quality.runtime import ROOT
from scripts.native_processes import Supervisor


def main() -> None:
    """Run the command-line workflow with the supplied options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evals-root", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.evals_root.resolve() / "src"))
    from evals_app.calibration import calibrate_business
    from evals_app.logs import load_trials
    from evals_app.models import (
        Application,
        Asset,
        Budget,
        Calibration,
        Case,
        Dimension,
        Plan,
        Rule,
    )
    from evals_app.service import Service

    directory, rows, version, cases, port, app = _prepare_application(
        args, Case, Rule, Dimension, Application, Asset
    )
    calibration, plan = _build_plan(
        app, cases, version, calibrate_business, Plan, Calibration, Budget
    )
    dump(
        directory / "manifest.json",
        {
            "plan": plan.model_dump(mode="json"),
            "calibration": calibration,
            "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
            "formal_release_eligible": False,
        },
    )
    _run_experiment(
        args, directory, rows, app, plan, calibration, port, version, Service, load_trials
    )


def _write_metrics(directory, rows, experiment, load_trials):
    metrics = []
    by_id = {row["id"]: row for row in rows}
    for key, log in experiment["runs"].items():
        trials = load_trials(log)
        for cohort in ("zh-general", "en-medical", "en-legal"):
            planned = [t for t in trials if by_id[t.sample_id]["cohort"] == cohort]
            observed = [t.observation.facts["retrieval"] for t in planned if t.observation]
            for metric in ("recall_at_10", "mrr", "ndcg_at_10"):
                values = [r[metric] for r in observed if metric in r]
                metrics.append(
                    {
                        "run": key,
                        "cohort": cohort,
                        "metric": metric,
                        "planned": len(planned),
                        "scored": len(values),
                        "missing": len(planned) - len(values),
                        "observed_mean": statistics.mean(values) if values else None,
                        "formal_release_eligible": False,
                    }
                )
    dump(directory / "metrics.json", metrics)
    return metrics


def _run_experiment(
    args, directory, rows, app, plan, calibration, port, version, service, load_trials
):
    supervisor = Supervisor(directory, print)
    service = None
    try:
        environment = dict(
            os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"
        )
        supervisor.start(
            "gateway",
            [
                ROOT / ".local-run/venvs/core-security/Scripts/python.exe",
                "-m",
                "evals.quality.http_gateway",
                "--run",
                args.run.resolve(),
                "--dataset",
                args.dataset.resolve(),
                "--cases",
                args.cases.resolve(),
                "--output",
                directory / "native",
                "--port",
                str(port),
                "--version",
                version,
            ],
            ROOT,
            environment,
        )
        supervisor.wait_http({"gateway": app.entrypoint}, timeout=60)
        service = service(directory / "workspace", background=False)
        service.register(app, key="application")
        service.store.update(
            lambda state: state["calibrations"].update(
                {calibration["calibration"]["artifact"]: calibration}
            )
        )
        created = service.create(plan, [], key="native-baseline", use_gepa=False)
        dump(directory / "experiment.json", created)
        service.start(created["id"], created["fingerprint"])
        service.run(created["id"])
        experiment = service.store.read()["experiments"][created["id"]]
        dump(directory / "result.json", experiment)
        metrics = _write_metrics(directory, rows, experiment, load_trials)
        print(
            json.dumps(
                {
                    "status": experiment["status"],
                    "conclusion": experiment.get("conclusion"),
                    "metrics": metrics,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if experiment["status"] != "completed":
            raise RuntimeError(
                "Evals experiment did not complete; retain its actual terminal state"
            )
    finally:
        if service:
            service.close()
        supervisor.close()
        dump(
            directory / "run-ended.json",
            {"owned_gateway_stopped": True, "result_exists": (directory / "result.json").exists()},
        )


def _build_plan(app, cases, version, calibrate_business, plan, calibration, budget):
    annotations = []
    case = cases[0]
    for value, label in ((True, "pass"), (False, "fail"), (None, "unverifiable")):
        evidence = {
            r.path: {
                "source": VERIFIER,
                "locator": "calibration:rule-boolean-boundaries-only",
                "version": version,
                "scope": "local",
                "collected_at": datetime.now(UTC).isoformat(),
            }
            for r in case.rules
        }
        observation = (
            None
            if value is None
            else {
                "output": "Rule-interpreter calibration only",
                "application_version": version,
                "facts": {"retrieval": {"passed": value}, "security": {"authorized": True}},
                "evidence": evidence,
            }
        )
        annotations.append(
            {
                "case": case.model_dump(mode="json"),
                "observation": observation,
                "expected": {
                    "retrieval": label,
                    "security": "unverifiable" if value is None else "pass",
                },
            }
        )
    calibration = calibrate_business(app, annotations, "native-http-boolean-boundaries-v1", 1.0)
    plan = plan(
        application=app,
        dataset_version=version,
        calibration=calibration.model_validate(calibration["calibration"]),
        cases=cases,
        objective="quality",
        minimum_gain=0.01,
        quality_tolerance=0,
        minimum_groups=3,
        epochs=1,
        budget=budget(application=0, grading=0, analysis=0, reflection=0),
        environment={
            "execution": "Actual native /search HTTP; K=10; no embedding, pair-score or answer replay cache",
            "objective": "Composite per-case business-rule pass rate; raw macro ranking metrics reported separately",
            "scope": "Baseline train and selection only; no proposed candidate, no holdout or counterexample execution",
            "calibration": "Deterministic interpreter boundaries only; not model calibration or human agreement",
            "gateway_sha256": hashlib.sha256(
                Path(__file__).with_name("http_gateway.py").read_bytes()
            ).hexdigest(),
        },
    )
    return calibration, plan


def _prepare_application(args, case, rule, dimension, application, asset):
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    rows = read_jsonl(args.cases)
    runtime = args.run / "retrieval-runtime-source.json"
    actual_settings = args.run / "retrieval-effective-settings.json"
    version = hashlib.sha256(
        (args.run / "run-manifest.json").read_bytes()
        + args.cases.read_bytes()
        + (runtime.read_bytes() if runtime.exists() else b"")
        + (actual_settings.read_bytes() if actual_settings.exists() else b"")
    ).hexdigest()
    cases = [
        case(
            id=r["id"],
            group=r["group"],
            split=r["split"],
            input=r["query"],
            metadata={"case_id": r["id"], "cohort": r["cohort"]},
            rules=[
                rule(
                    dimension=dimension.RETRIEVAL,
                    path="retrieval.passed",
                    expected=True,
                    source=VERIFIER,
                ),
                rule(
                    dimension=dimension.SECURITY,
                    path="security.authorized",
                    expected=True,
                    critical=True,
                    source=VERIFIER,
                ),
            ],
        )
        for r in rows
    ]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    os.environ["NEXRAG_QUALITY_EVALS_TOKEN"] = secrets.token_hex(32)
    app = application(
        id="nexrag-native-http-k10",
        version=version,
        kind="rag",
        adapter="http",
        entrypoint=f"http://127.0.0.1:{port}",
        identity_ref="NEXRAG_QUALITY_EVALS_TOKEN",
        provenance="real",
        data_source="Entire official CMRC contexts, PubMedQA provided contexts and CUAD originals via production upload API",
        observable=[dimension.RETRIEVAL, dimension.SECURITY],
        assets={"k": asset(kind="choice", values=[10])},
        config={"k": 10},
        timeout=180,
        estimate_per_attempt=0,
    )
    return directory, rows, version, cases, port, app


if __name__ == "__main__":
    main()
