"""提案生命周期、积压治理、审批、金丝雀、晋升与回滚。

状态流转：
    pending_review --approve（批准）--> canary --promote（晋升）--> promoted
          |                       |--自动/手动 rollback--> rolled_back
          |--reject（驳回）--> rejected
          |--基线变化--> stale --重新评估--> pending_review | closed
          |--超过 TTL--> expired
          |--并入更新的证据--> （仍是同一提案，evidence_count 增加）

积压规则：
- 每种产物 kind 只允许一个未决提案（pending_review/stale/canary）；同一聚类的
  新候选只刷新现有提案的证据；
- 审核队列有上限（PROPOSAL_QUEUE_LIMIT）；超出后聚类仍会记录，
  但不再生成新提案；
- 没有管理员批准，任何改动都不会上线。
"""

from __future__ import annotations

import json
from typing import Any

from src.config import settings
from src.evolution.artifacts import ArtifactRegistry, registry
from src.evolution.canary import enough_samples, metrics_for_proposal, regressed_metrics
from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now

OPEN = ("pending_review", "stale", "canary")


class GovernanceError(ValueError):
    """生命周期转换不被允许时抛出。"""


class Governance:
    def __init__(self, store: OpsStore | None = None, reg: ArtifactRegistry | None = None):
        self._store = store
        self.reg = reg or registry

    @property
    def store(self) -> OpsStore:
        return self._store or get_ops_store()

    # ----------------------------------------------------------------- 查询
    def get(self, proposal_id: str) -> dict[str, Any] | None:
        """按 ID 取提案（报告已解析为字典）；不存在返回 None。"""
        row = self.store.query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return {**row, "report": json.loads(row["report"])} if row else None

    def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """列出提案；指定状态时按优先级排序，否则按最近更新排序，最多返回 limit 条。"""
        if status:
            rows = self.store.query(
                "SELECT * FROM proposals WHERE status = ? ORDER BY priority DESC, created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM proposals ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        return [{**row, "report": json.loads(row["report"])} for row in rows]

    def open_for_kind(self, kind: str) -> dict[str, Any] | None:
        """返回该产物类型最新的未决提案（待审核、基线已变动待重新评估或灰度中），没有则返回 None。"""
        marks = ",".join("?" for _ in OPEN)
        row = self.store.query_one(
            f"SELECT * FROM proposals WHERE kind = ? AND status IN ({marks}) ORDER BY created_at DESC LIMIT 1",
            (kind, *OPEN),
        )
        return {**row, "report": json.loads(row["report"])} if row else None

    def queue_size(self) -> int:
        """返回待审核提案的数量。"""
        row = self.store.query_one(
            "SELECT COUNT(*) AS n FROM proposals WHERE status = 'pending_review'"
        )
        return int(row["n"]) if row else 0

    def backlog_status(self) -> dict[str, Any]:
        """汇总提案积压：各状态数量、队列上限与是否已满、最早待审核提案的天数，以及是否超过 TTL 一半的告警。"""
        counts = {
            row["status"]: row["n"]
            for row in self.store.query(
                "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status"
            )
        }
        oldest = self.store.query_one(
            "SELECT MIN(created_at) AS t FROM proposals WHERE status = 'pending_review'"
        )
        age_days = (now() - oldest["t"]) / 86_400 if oldest and oldest["t"] else 0.0
        return {
            "counts": counts,
            "queue_limit": settings.PROPOSAL_QUEUE_LIMIT,
            "queue_full": counts.get("pending_review", 0) >= settings.PROPOSAL_QUEUE_LIMIT,
            "oldest_pending_days": round(age_days, 1),
            "alert": age_days > settings.PROPOSAL_TTL_DAYS / 2,
        }

    # ------------------------------------------------------------ 状态转换
    def submit(
        self,
        *,
        kind: str,
        cluster_key: str,
        candidate_id: str,
        baseline_id: str,
        evidence_count: int,
        report: dict[str, Any],
        priority: float,
    ) -> tuple[str, str]:
        """创建或合并提案，返回 (proposal_id, action)。"""
        existing = self.open_for_kind(kind)
        at = now()
        if existing and existing["cluster_key"] == cluster_key and existing["status"] != "canary":
            # 同一问题再次出现：刷新证据和候选，而不是新建提案。
            self.reg.set_label(existing["candidate_id"], "rejected")
            self.store.execute(
                "UPDATE proposals SET candidate_id = ?, baseline_id = ?, evidence_count = ?, report = ?, "
                "priority = ?, status = 'pending_review', updated_at = ? WHERE id = ?",
                (
                    candidate_id,
                    baseline_id,
                    evidence_count,
                    dumps(report),
                    priority,
                    at,
                    existing["id"],
                ),
            )
            return existing["id"], "merged"
        if existing:
            # 该产物已有其他未决提案：不叠加修改。
            self.reg.set_label(candidate_id, "rejected")
            return existing["id"], "deferred"
        if self.queue_size() >= settings.PROPOSAL_QUEUE_LIMIT:
            self.reg.set_label(candidate_id, "rejected")
            return "", "queue_full"
        proposal_id = new_id()
        self.store.execute(
            "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                kind,
                cluster_key,
                candidate_id,
                baseline_id,
                "pending_review",
                evidence_count,
                dumps(report),
                priority,
                None,
                None,
                at,
                at,
                None,
            ),
        )
        return proposal_id, "created"

    def _transition(
        self, proposal_id: str, allowed: tuple[str, ...], status: str, **fields: Any
    ) -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if not proposal:
            raise GovernanceError("Unknown proposal")
        if proposal["status"] not in allowed:
            raise GovernanceError(f"Cannot move a {proposal['status']} proposal to {status}")
        assignments = ", ".join(["status = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields])
        self.store.execute(
            f"UPDATE proposals SET {assignments} WHERE id = ?",
            (status, now(), *fields.values(), proposal_id),
        )
        return proposal

    def approve(self, proposal_id: str, reviewer: str, note: str | None = None) -> None:
        """人工批准只启动金丝雀，绝不直接晋升。"""
        proposal = self.get(proposal_id)
        if proposal and proposal["baseline_id"] != self.reg.production(proposal["kind"]).id:
            self._transition(proposal_id, ("pending_review",), "stale")
            raise GovernanceError("Production changed since evaluation; proposal marked stale")
        proposal = self._transition(
            proposal_id,
            ("pending_review",),
            "canary",
            reviewer=reviewer,
            review_note=note,
            canary_started_at=now(),
        )
        self.reg.set_label(proposal["candidate_id"], "staging")

    def reject(self, proposal_id: str, reviewer: str, note: str | None = None) -> None:
        """驳回待审核或已过期的提案并废弃其候选版本；其他状态抛出 GovernanceError。"""
        proposal = self._transition(
            proposal_id,
            ("pending_review", "stale"),
            "rejected",
            reviewer=reviewer,
            review_note=note,
        )
        self.reg.set_label(proposal["candidate_id"], "rejected")

    def promote(self, proposal_id: str, reviewer: str) -> None:
        """第二次人工确认。灰度指标一律由运营库重新计算，调用方无法传入摘要绕过门控。"""
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "canary":
            raise GovernanceError("Only a proposal in canary can be promoted")
        elapsed_days = (now() - (proposal["canary_started_at"] or now())) / 86_400
        if elapsed_days < settings.CANARY_MIN_DAYS:
            raise GovernanceError(
                f"Canary has run {elapsed_days:.1f} of {settings.CANARY_MIN_DAYS} days"
            )
        if self.reg.production(proposal["kind"]).id != proposal["baseline_id"]:
            raise GovernanceError(
                "Production changed during canary; evaluate again against the current baseline"
            )
        canary_metrics = metrics_for_proposal(self.store, proposal)
        if not enough_samples(canary_metrics):
            raise GovernanceError(
                "Canary requires at least 20 candidate and 20 control observations"
            )
        if regressed_metrics(canary_metrics, settings.CANARY_MAX_DROP):
            raise GovernanceError("Canary performance regressed; promotion is blocked")
        candidate = self.reg.get(proposal["candidate_id"])
        if candidate is None:
            raise GovernanceError("The candidate artifact no longer exists")
        # 记录谱系，使回滚能恢复到确切的前任版本。
        self.store.execute(
            "UPDATE artifact_versions SET parent_id = ? WHERE id = ?",
            (proposal["baseline_id"], candidate.id),
        )
        self.reg.set_label(candidate.id, "production")
        report = {**proposal["report"], "canary_metrics": canary_metrics}
        self._transition(
            proposal_id, ("canary",), "promoted", reviewer=reviewer, report=dumps(report)
        )

    def rollback(self, proposal_id: str, reason: str, reviewer: str = "system") -> None:
        """终止金丝雀，或把已晋升的版本回滚到其前任。"""
        proposal = self.get(proposal_id)
        if not proposal:
            raise GovernanceError("Unknown proposal")
        if proposal["status"] == "canary":
            self.reg.set_label(proposal["candidate_id"], "rejected")
        elif proposal["status"] == "promoted":
            self.reg.rollback(proposal["kind"])
        else:
            raise GovernanceError("Only canary or promoted proposals can be rolled back")
        self._transition(
            proposal_id,
            ("canary", "promoted"),
            "rolled_back",
            reviewer=reviewer,
            review_note=reason,
        )

    def housekeeping(self) -> dict[str, int]:
        """使过期提案失效，并把基线已变动的提案标记为 stale。"""
        at = now()
        expired = 0
        for proposal in self.list("pending_review") + self.list("stale"):
            if at - proposal["created_at"] > settings.PROPOSAL_TTL_DAYS * 86_400:
                self._transition(proposal["id"], ("pending_review", "stale"), "expired")
                self.reg.set_label(proposal["candidate_id"], "rejected")
                expired += 1
        stale = 0
        for proposal in self.list("pending_review"):
            if proposal["baseline_id"] != self.reg.production(proposal["kind"]).id:
                self._transition(proposal["id"], ("pending_review",), "stale")
                stale += 1
        return {"expired": expired, "stale": stale}
