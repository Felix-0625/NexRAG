"""带版本、可演化的配置产物（prompt、params）。

自进化只允许修改这些产物；代码以及 ``src/agent/prompts.py`` 中的安全前缀
永远不会被演化循环写入。标签沿用 Langfuse 的 prompt 管理约定：
``production`` 承接线上流量，``staging`` 服务金丝雀分组，
``candidate`` 等待评估/审核，``archived`` 保留历史以便回滚。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.agent.prompts import DEFAULT_GUIDANCE
from src.config import settings
from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now

KINDS = ("prompt", "params")
LABELS = ("candidate", "staging", "production", "archived", "rejected")

# 演化循环可调的参数及其允许范围。
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "retrieval_min_relevance": (0.10, 0.80),
    "max_tool_retries": (0, 4),
    "prune_max_tables": (1, 4),
    "knowledge_top_k": (1, 3),
}


def default_params() -> dict[str, float]:
    """取自部署设置的基线参数值。"""
    return {
        "retrieval_min_relevance": settings.RETRIEVAL_MIN_RELEVANCE,
        "max_tool_retries": settings.MAX_TOOL_RETRIES,
        "prune_max_tables": 2,
        "knowledge_top_k": 3,
    }


def clamp_params(params: dict[str, Any]) -> dict[str, float]:
    """丢弃未知键，并把取值限制在 PARAM_BOUNDS 范围内。"""
    clean: dict[str, float] = {}
    for key, (low, high) in PARAM_BOUNDS.items():
        if key in params:
            value = float(params[key])
            value = min(max(value, low), high)
            clean[key] = int(value) if isinstance(low, int) and isinstance(high, int) else value
    return clean


@dataclass(frozen=True)
class Artifact:
    id: str
    kind: str
    content: Any
    label: str
    parent_id: str | None
    note: str | None


class ArtifactRegistry:
    """保存产物版本并管理线上、灰度与归档标签。"""

    def __init__(self, store: OpsStore | None = None):
        self._store = store

    @property
    def store(self) -> OpsStore:
        return self._store or get_ops_store()

    @staticmethod
    def _row_to_artifact(row: dict[str, Any]) -> Artifact:
        return Artifact(
            id=row["id"],
            kind=row["kind"],
            content=json.loads(row["content"]),
            label=row["label"],
            parent_id=row["parent_id"],
            note=row["note"],
        )

    def create(
        self,
        kind: str,
        content: Any,
        *,
        label: str = "candidate",
        parent_id: str | None = None,
        note: str | None = None,
        created_by: str = "system",
    ) -> Artifact:
        """保存一个新的不可变产物版本。"""
        if kind not in KINDS or label not in LABELS:
            raise ValueError(f"Unsupported artifact kind/label: {kind}/{label}")
        if kind == "params":
            content = clamp_params(content)
        artifact_id = new_id()
        self.store.execute(
            "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?)",
            (artifact_id, kind, dumps(content), parent_id, label, note, created_by, now()),
        )
        return Artifact(artifact_id, kind, content, label, parent_id, note)

    def get(self, artifact_id: str) -> Artifact | None:
        """按 ID 取产物版本，不存在返回 None。"""
        row = self.store.query_one("SELECT * FROM artifact_versions WHERE id = ?", (artifact_id,))
        return self._row_to_artifact(row) if row else None

    def by_label(self, kind: str, label: str) -> Artifact | None:
        """取该类型下带指定标签的最新产物版本，没有则返回 None。"""
        row = self.store.query_one(
            "SELECT * FROM artifact_versions WHERE kind = ? AND label = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (kind, label),
        )
        return self._row_to_artifact(row) if row else None

    def production(self, kind: str) -> Artifact:
        """返回当前线上版本；首次使用时写入内置默认值。"""
        current = self.by_label(kind, "production")
        if current:
            if kind == "prompt":
                from src.evolution.builtin_migrations import upgrade_shared_knowledge_default

                upgrade_shared_knowledge_default(self.store, current)
                # 可能已有其他进程完成了同一迁移。
                return self.by_label(kind, "production") or current
            return current
        with self.store.transaction(immediate=True):
            # 不同线程或进程首次访问时，等待者须重新读取已写入的默认版本。
            current = self.by_label(kind, "production")
            if current:
                return current
            content = DEFAULT_GUIDANCE if kind == "prompt" else default_params()
            return self.create(kind, content, label="production", note="built-in default")

    def set_label(self, artifact_id: str, label: str) -> None:
        """把版本移到新标签；每种 kind 至多有一个 production/staging。"""
        artifact = self.get(artifact_id)
        if not artifact or label not in LABELS:
            raise ValueError("Unknown artifact or label")
        with self.store.transaction() as conn:
            if label in {"production", "staging"}:
                conn.execute(
                    "UPDATE artifact_versions SET label = 'archived' "
                    "WHERE kind = ? AND label = ? AND id != ?",
                    (artifact.kind, label, artifact_id),
                )
            conn.execute(
                "UPDATE artifact_versions SET label = ? WHERE id = ?", (label, artifact_id)
            )

    def history(self, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        """列出该类型最近的版本元数据（不含内容），新的在前，最多 limit 条。"""
        return self.store.query(
            "SELECT id, kind, label, parent_id, note, created_by, created_at "
            "FROM artifact_versions WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
            (kind, limit),
        )


registry = ArtifactRegistry()
