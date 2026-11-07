"""一次自进化周期（批处理，绝不按单轮对话触发）。

清理 -> 挖掘失败 -> 可处理的聚类（规模 >= 下限）
  -> 对每种没有未决提案且队列有空位的产物 kind：
       生成候选（仅使用 train 数据）
       在 train + holdout + regression 上对比基线与候选
       过门禁 -> 提交带变更报告的提案（或记录拒绝）
金丝雀监控 -> 退化时自动回滚，标记可晋升的提案
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.config import settings
from src.evaluation import datasets
from src.evaluation.runner import ExperimentResult, Task, agent_task, run_experiment
from src.evolution.artifacts import KINDS, ArtifactRegistry
from src.evolution.canary import enough_samples, metrics_for_proposal, regressed_metrics
from src.evolution.gate import LOWER_IS_BETTER, evaluate_gate
from src.evolution.governance import Governance
from src.evolution.miner import FailureCluster, actionable, mine_failures
from src.evolution.proposer import (
    Completer,
    cluster_evidence,
    focus_params,
    param_neighbours,
    prompt_diff,
    propose_prompt,
)
from src.midplatform.ops_store import OpsStore, get_ops_store, now

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    clusters: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    housekeeping: dict[str, int] = field(default_factory=dict)
    canary: list[dict[str, Any]] = field(default_factory=list)


def _change_report(
    kind: str,
    cluster: FailureCluster,
    baseline,
    candidate,
    base_run,
    cand_run,
    verdict,
    rationale: str,
    evidence,
    baseline_versions: dict[str, str],
) -> dict[str, Any]:
    if kind == "prompt":
        change = {"diff": prompt_diff(baseline.content, candidate.content)}
    else:
        change = {
            "params": {
                key: {"from": baseline.content.get(key), "to": value}
                for key, value in candidate.content.items()
                if baseline.content.get(key) != value
            }
        }
    return {
        "what_changed": change,
        "why": {
            "cluster": cluster.key,
            "reason": cluster.reason,
            "tool": cluster.tool,
            "trace_count": cluster.size,
            "rationale": rationale,
            "examples": [
                {
                    "trace_id": e["id"],
                    "input": (e["input_text"] or "")[:200],
                    "errors": e["errors"][:2],
                }
                for e in evidence
            ],
        },
        "effect": {
            "baseline": base_run.metrics,
            "candidate": cand_run.metrics,
            "baseline_run_id": base_run.run_id,
            "candidate_run_id": cand_run.run_id,
            "baseline_versions": baseline_versions,
        },
        "gate": verdict.checks,
        "risk": _risk(base_run.metrics, cand_run.metrics),
        "rollback": {"to_version": baseline.id, "how": "POST /v1/ops/proposals/{id}/rollback"},
    }


def _risk(base: dict, cand: dict) -> list[str]:
    notes = []
    for split, metrics in cand.items():
        for metric, value in metrics.items():
            before = base.get(split, {}).get(metric)
            if before is None or metric == "n":
                continue
            if metric == "latency_ms" and before and (value - before) / before > 0.1:
                notes.append(f"{split}: latency +{(value - before) / before:.0%}")
            elif metric not in LOWER_IS_BETTER and value < before - 1e-9:
                notes.append(f"{split}: {metric} {before:.3f} -> {value:.3f}")
    return notes or ["no metric moved in the wrong direction"]


class EvolutionCycle:
    """从合成训练失败生成候选，经独立分组评测后提交治理层；不直接改生产版本。"""

    def __init__(
        self,
        *,
        completer: Completer | None = None,
        task: Task = agent_task,
        store: OpsStore | None = None,
        reg: ArtifactRegistry | None = None,
    ):
        self.completer = completer
        self.task = task
        self._store = store
        self.reg = reg or ArtifactRegistry(store)
        self.gov = Governance(store, self.reg)

    @property
    def store(self) -> OpsStore:
        return self._store or get_ops_store()

    async def _evaluate(
        self, name: str, guidance: str, params: dict, ids: dict, items: list[datasets.DatasetItem]
    ) -> ExperimentResult:
        return await run_experiment(
            name,
            items,
            guidance=guidance,
            params=params,
            artifact_ids=ids,
            repeats=settings.EVOLUTION_REPEATS,
            task=self.task,
            store=self.store,
        )

    async def _best_params(
        self, baseline, cluster: FailureCluster, train_items, prompt
    ) -> tuple[dict | None, str]:
        """只在 train 上挑选最佳的一步邻居（holdout 保持不可见）。"""
        base = await self._evaluate(
            "params-search-base", prompt.content, baseline.content, {}, train_items
        )
        best, best_q = None, base.metrics.get("train", {}).get("quality", 0.0)
        for neighbour in param_neighbours(baseline.content, focus_params(cluster)):
            run = await self._evaluate("params-search", prompt.content, neighbour, {}, train_items)
            q = run.metrics.get("train", {}).get("quality", 0.0)
            if q > best_q + 1e-9:
                best, best_q = neighbour, q
        return (
            best,
            f"train quality {base.metrics.get('train', {}).get('quality', 0):.3f} -> {best_q:.3f}",
        )

    async def run(self) -> CycleReport:
        """执行一次完整周期：清理过期提案、监控灰度、挖掘失败聚类，再为 prompt 和 params 各生成候选并过门禁。

        没有达标的聚类或缺少 holdout 条目时直接跳过；产物已有灰度中的提案或审核队列已满时也跳过。
        通过门禁的候选只提交为待审核提案，返回 CycleReport。
        """
        report = CycleReport(housekeeping=self.gov.housekeeping())
        report.canary = self.monitor_canaries()
        clusters = mine_failures(self.store)
        report.clusters = [
            {"key": c.key, "size": c.size, "weight": round(c.weight, 2)} for c in clusters
        ]
        targets = actionable(clusters)
        if not targets:
            report.actions.append(
                {"action": "none", "detail": "no cluster reached the minimum size"}
            )
            return report

        gate_items = datasets.items(datasets.SPLITS, self.store)
        train = [i for i in gate_items if i.split == "train"]
        regression_ids = [i.id for i in gate_items if i.split == "regression"]
        missing = set(datasets.SPLITS) - {i.split for i in gate_items}
        if missing:
            report.actions.append(
                {
                    "action": "skipped",
                    "detail": "missing evaluation splits: " + ", ".join(sorted(missing)),
                }
            )
            return report

        for kind in KINDS:
            open_proposal = self.gov.open_for_kind(kind)
            if open_proposal and open_proposal["status"] == "canary":
                report.actions.append(
                    {"kind": kind, "action": "skipped", "detail": "canary in progress"}
                )
                continue
            if self.gov.queue_size() >= settings.PROPOSAL_QUEUE_LIMIT:
                report.actions.append(
                    {"kind": kind, "action": "skipped", "detail": "review queue full"}
                )
                continue
            cluster = targets[0]
            report.actions.append(
                await self._propose(kind, cluster, train, gate_items, regression_ids)
            )
        return report

    async def _propose(self, kind, cluster, train, gate_items, regression_ids) -> dict[str, Any]:
        prompt = self.reg.production("prompt")
        params = self.reg.production("params")
        baseline = prompt if kind == "prompt" else params
        if kind == "prompt":
            if self.completer is None:
                return {"kind": kind, "action": "skipped", "detail": "no LLM completer configured"}
            proposed = await propose_prompt(prompt.content, cluster, self.completer, self.store)
            if not proposed:
                return {"kind": kind, "action": "no_candidate", "cluster": cluster.key}
            content, rationale = proposed
        else:
            content, rationale = await self._best_params(params, cluster, train, prompt)
            if content is None:
                return {
                    "kind": kind,
                    "action": "no_candidate",
                    "cluster": cluster.key,
                    "detail": rationale,
                }

        candidate = self.reg.create(
            kind,
            content,
            parent_id=baseline.id,
            note=f"cluster {cluster.key}",
            created_by="evolution",
        )
        cand_prompt = candidate.content if kind == "prompt" else prompt.content
        cand_params = candidate.content if kind == "params" else params.content
        baseline_ids = {"prompt": prompt.id, "params": params.id}
        base_run = await self._evaluate(
            f"{kind}-baseline",
            prompt.content,
            params.content,
            baseline_ids,
            gate_items,
        )
        cand_run = await self._evaluate(
            f"{kind}-candidate",
            cand_prompt,
            cand_params,
            {**baseline_ids, kind: candidate.id},
            gate_items,
        )
        verdict = evaluate_gate(base_run, cand_run, regression_item_ids=regression_ids)
        evidence = cluster_evidence(cluster, self.store)
        change_report = _change_report(
            kind,
            cluster,
            baseline,
            candidate,
            base_run,
            cand_run,
            verdict,
            rationale,
            evidence,
            baseline_ids,
        )
        if not verdict.passed:
            self.reg.set_label(candidate.id, "rejected")
            return {
                "kind": kind,
                "action": "gate_rejected",
                "cluster": cluster.key,
                "gate": verdict.checks,
            }
        gain = cand_run.metrics["holdout"]["quality"] - base_run.metrics["holdout"]["quality"]
        proposal_id, action = self.gov.submit(
            kind=kind,
            cluster_key=cluster.key,
            candidate_id=candidate.id,
            baseline_id=baseline.id,
            evidence_count=cluster.size,
            report=change_report,
            priority=round(cluster.weight * max(gain, 0), 4),
        )
        return {"kind": kind, "action": action, "proposal_id": proposal_id, "cluster": cluster.key}

    def canary_metrics(self, proposal: dict[str, Any]) -> dict[str, float]:
        """对比金丝雀启动以来 canary 与 production 的线上 trace。"""
        return metrics_for_proposal(self.store, proposal)

    def monitor_canaries(self) -> list[dict[str, Any]]:
        """自动回滚表现不佳的金丝雀；报告可以晋升的金丝雀。"""
        results = []
        for proposal in self.gov.list("canary"):
            metrics = self.canary_metrics(proposal)
            if not enough_samples(metrics):
                results.append({"proposal_id": proposal["id"], "state": "collecting", **metrics})
                continue
            dropped = regressed_metrics(metrics, settings.CANARY_MAX_DROP)
            if dropped:
                if self.gov.rollback(
                    proposal["id"], f"canary regression on {', '.join(dropped)}", canary_only=True
                ):
                    results.append(
                        {"proposal_id": proposal["id"], "state": "rolled_back", **metrics}
                    )
                continue
            elapsed = (now() - (proposal["canary_started_at"] or now())) / 86_400
            state = "ready_to_promote" if elapsed >= settings.CANARY_MIN_DAYS else "observing"
            results.append({"proposal_id": proposal["id"], "state": state, **metrics})
        return results
