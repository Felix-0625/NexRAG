"""用固定的产物版本让 Agent 跑数据集条目（Langfuse 的 "experiment"）。

runner 从不改动线上标签：它只评估显式给定的 prompt/params 内容，
因此候选与 production 可在相同条目、模型和温度下比较。结果以 experiment run 存储。
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
    run_id: str
    metrics: dict[str, dict[str, float]]  # 划分 -> 指标 -> 均值
    item_scores: dict[str, dict[str, float]] = field(default_factory=dict)


def outcome_from_messages(
    answer: str, messages, *, latency_ms: float, status: str = "ok"
) -> RunOutcome:
    """从一次运行的消息历史中收集工具、SQL 行、来源和错误。"""
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


# task 把 (条目, prompt 指引, 参数) 映射为 RunOutcome。默认 task 运行真实 Agent；
# 测试则注入确定性的 task。
Task = Callable[[DatasetItem, str, dict[str, Any]], Awaitable[RunOutcome]]


async def agent_task(item: DatasetItem, guidance: str, params: dict[str, Any]) -> RunOutcome:
    """用给定的 prompt 与参数在隔离的合成 fixture 上运行真实 Agent，返回可打分的结果。

    非合成、带来源对话或租户不在固定合成租户内的条目一律拒绝（返回 error 结果），不会接触真实业务数据；
    运行失败也作为结果返回，并记录源库是否被改动。
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
    except Exception as exc:  # noqa: BLE001 - 失败的运行也是一个会被打分的结果
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
    """评估一组产物配置；重复运行的结果按条目取平均。"""
    store = store or get_ops_store()
    per_item: dict[str, dict[str, float]] = {}
    for item in items:
        runs = []
        for _ in range(max(1, repeats)):
            outcome = await task(item, guidance, params)
            scores = score_item(item.expected, outcome)
            scores["quality"] = quality(scores)
            runs.append(scores)
        names = {key for run in runs for key in run}
        per_item[item.id] = {
            key: statistics.fmean(run[key] for run in runs if key in run) for key in names
        }
        # 安全取最坏情况：任何一次重复违规都计入。
        per_item[item.id]["safety_violation"] = max(run["safety_violation"] for run in runs)
        if record_synthetic_failures:
            from src.evaluation.synthetic import record_synthetic_failure

            record_synthetic_failure(item, outcome, per_item[item.id]["quality"], store)
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
    """面向管理后台的安全入口：固定 fixture、本地模型、不接触真实数据。

    UI 只能选择已知的数据划分和已注册的本地模型，不能传入
    task callable、adapter、数据源路径、知识范围或 DSN。
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
