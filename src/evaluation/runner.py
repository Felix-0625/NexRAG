"""Evaluate dataset entries against pinned artifacts without changing production labels.

Compare candidate and production guidance and parameters on identical entries, models,
and temperatures; persist outcomes as experiment runs.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from pydantic_ai.messages import ModelRequest, ToolReturnPart

from src.evaluation.datasets import DatasetItem
from src.evaluation.evaluators import RunOutcome, quality, score_item
from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now


@dataclass
class ExperimentResult:
    """Per-entry outcomes and split-level aggregate experiment scores."""

    run_id: str
    metrics: dict[str, dict[str, float]]
    item_scores: dict[str, dict[str, float]] = field(default_factory=dict)


def outcome_from_messages(
    answer: str, messages, *, latency_ms: float, status: str = "ok"
) -> RunOutcome:
    """Collect tools, SQL rows, sources, and failures from a run's message history."""
    outcome = RunOutcome(answer=answer or "", latency_ms=latency_ms, status=status)
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, ToolReturnPart):
                continue
            outcome.tools.append(part.tool_name)
            content = part.content if isinstance(part.content, dict) else {}
            if content.get("error"):
                outcome.tool_errors += 1
                if (
                    "denied" in str(content["error"]).lower()
                    or "not available" in str(content["error"]).lower()
                ):
                    outcome.denied = True
            if isinstance(content.get("rows"), list) and not content.get("error"):
                outcome.sql_rows.append(content["rows"])
            for doc in content.get("documents") or []:
                if isinstance(doc, dict) and doc.get("source"):
                    outcome.sources.append(str(doc["source"]))
    return outcome


# The default task maps entries, guidance, and parameters to real-agent outcomes;
# tests inject deterministic tasks instead.
Task = Callable[[DatasetItem, str, dict[str, Any]], Awaitable[RunOutcome]]


async def agent_task(item: DatasetItem, guidance: str, params: dict[str, Any]) -> RunOutcome:
    """Run the real agent on an isolated synthetic fixture using explicit guidance and parameters.

    Reject nonsynthetic entries, conversation provenance, and tenants outside the fixed
    fixture. Return failures as scoreable outcomes and record source integrity.
    """
    from src.agent.agent import UserContext, runner
    from src.agent.execution import EvaluationRun
    from src.evaluation.fixtures import DOCUMENTS, SCHEMA_TEXT, TENANT_IDS, SyntheticFixture

    if item.provenance != "synthetic" or item.origin_trace_id or item.tenant_id not in TENANT_IDS:
        return RunOutcome(
            "Shared evaluations require an isolated synthetic fixture; live business evaluation needs a separately authorized private runner.",
            status="error",
        )

    context = UserContext(
        user_id="eval-runner",
        email="eval@nexrag.local",
        tenant_id=item.tenant_id,
        knowledge_scopes=[],
        model_id=item.expected.get("model_id"),
    )
    execution = EvaluationRun(guidance, params, list(item.expected.get("required_tools", [])))
    fixture = SyntheticFixture()
    execution.authorized_tenants = (item.tenant_id,)
    execution.business_adapter = fixture.adapter
    execution.business_schema = SCHEMA_TEXT
    execution.knowledge_documents = DOCUMENTS
    started = time.monotonic()
    try:
        answer = await runner.run(item.input, context, evaluation=execution)
    except Exception as exc:  # noqa: BLE001 - Failed runs are scoreable outcomes.
        outcome = outcome_from_messages(
            str(exc),
            execution.messages,
            latency_ms=(time.monotonic() - started) * 1000,
            status="error",
        )
    else:
        outcome = outcome_from_messages(
            answer,
            execution.messages,
            latency_ms=(time.monotonic() - started) * 1000,
            status=execution.status,
        )
        outcome.denied |= execution.denied
    finally:
        if "outcome" in locals():
            outcome.source_unchanged = fixture.unchanged()
        fixture.close()
    outcome.synthetic_fixture = True
    return outcome


def _aggregate(
    per_item: dict[str, dict[str, float]], items: list[DatasetItem]
) -> dict[str, dict[str, float]]:
    by_split: dict[str, list[dict[str, float]]] = {}
    for item in items:
        if item.id in per_item:
            by_split.setdefault(item.split, []).append(per_item[item.id])
    metrics: dict[str, dict[str, float]] = {}
    for split, rows in by_split.items():
        names = {name for row in rows for name in row}
        metrics[split] = {
            name: round(statistics.fmean(row[name] for row in rows if name in row), 4)
            for name in sorted(names)
        }
        metrics[split]["quality"] = round(statistics.fmean(row["quality"] for row in rows), 4)
        metrics[split]["n"] = float(len(rows))
    return metrics


async def run_experiment(
    name: str,
    items: list[DatasetItem],
    *,
    guidance: str,
    params: dict[str, Any],
    artifact_ids: dict[str, str | None],
    repeats: int = 1,
    task: Task = agent_task,
    store: OpsStore | None = None,
    record_synthetic_failures: bool = False,
) -> ExperimentResult:
    """Evaluate an artifact configuration, averaging repeated runs per entry."""
    store = store or get_ops_store()
    per_item: dict[str, dict[str, float]] = {}
    for item in items:
        runs = []
        worst_outcome = None
        worst_quality = float("inf")
        for _ in range(max(1, repeats)):
            outcome = await task(item, guidance, params)
            scores = score_item(item.expected, outcome)
            scores["quality"] = quality(scores)
            runs.append(scores)
            if record_synthetic_failures and scores["quality"] < worst_quality:
                worst_outcome, worst_quality = outcome, scores["quality"]
        names = {key for run in runs for key in run}
        per_item[item.id] = {
            key: statistics.fmean(run[key] for run in runs if key in run) for key in names
        }
        # Use the worst safety outcome: any repeated violation counts.
        per_item[item.id]["safety_violation"] = max(run["safety_violation"] for run in runs)
        if worst_outcome is not None:
            from src.evaluation.synthetic import record_synthetic_failure

            # Retain evidence from an actual failed run even if the last retry passed.
            record_synthetic_failure(item, worst_outcome, per_item[item.id]["quality"], store)
    metrics = _aggregate(per_item, items)
    run_id = new_id()
    store.execute(
        "INSERT INTO experiment_runs VALUES (?,?,?,?,?,?)",
        (run_id, name, dumps(artifact_ids), dumps(metrics), dumps(per_item), now()),
    )
    return ExperimentResult(run_id, metrics, per_item)


async def run_synthetic_experiment(
    *,
    splits: tuple[str, ...] = ("train", "holdout", "regression"),
    repeats: int = 1,
    model_id: str | None = None,
    store: OpsStore | None = None,
) -> ExperimentResult:
    """Expose a safe administrative evaluation entry point with fixed fixtures and local models.

    The UI selects known splits and registered local models, never task callables, adapters,
    source paths, knowledge scopes, or DSNs.
    """
    from src.evaluation import datasets
    from src.evaluation.validation import validate_expected
    from src.evolution.artifacts import ArtifactRegistry
    from src.midplatform.agent_models import resolve_model
    from src.midplatform.data_policy import model_is_local

    if not splits or any(split not in datasets.SPLITS for split in splits) or not 1 <= repeats <= 3:
        raise ValueError("Invalid synthetic evaluation options")
    model = resolve_model(model_id)
    if not model_is_local(model):
        raise ValueError("Synthetic acceptance currently requires a registered local model")
    store = store or get_ops_store()
    selected = [
        replace(item, expected={**validate_expected(item.expected), "model_id": model["id"]})
        for item in datasets.items(splits, store)
        if item.provenance == "synthetic" and not item.origin_trace_id
    ]
    if not selected:
        raise ValueError("No isolated synthetic samples in the selected splits")
    registry = ArtifactRegistry(store)
    prompt, params = registry.production("prompt"), registry.production("params")
    return await run_experiment(
        "synthetic-admin",
        selected,
        guidance=prompt.content,
        params=params.content,
        artifact_ids={"prompt": prompt.id, "params": params.id, "model": model["id"]},
        repeats=repeats,
        record_synthetic_failures=True,
        store=store,
    )
