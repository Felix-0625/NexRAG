"""长期记忆的数据模型：作用域、类型、来源与状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCOPES = ("global", "tenant", "session")
KINDS = ("fact", "preference", "profile", "task_state", "episode")
# 等级越高越可信；推断出的记忆永远不能覆盖用户明确表述的记忆。
SOURCE_RANK = {"agent_inferred": 0, "user_implicit": 1, "user_explicit": 2}
# 只有这些类型可以跨租户召回（来自 global 作用域）。
CROSS_TENANT_KINDS = ("preference", "profile")
SCOPE_WEIGHT = {"session": 1.0, "tenant": 0.85, "global": 0.7}


@dataclass(frozen=True)
class MemoryScope:
    """由服务端推导的可见范围，绝不由 LLM 提供。"""

    user_id: str
    tenant_id: str | None = None
    chat_id: str | None = None


@dataclass
class MemoryCandidate:
    """从用户自己的话语中提取出的事实，尚未经过冲突消解。"""

    scope: str
    kind: str
    subject: str
    attribute: str
    value_text: str
    confidence: float
    source: str
    importance: float
    evidence_ref: str | None = None

    def slot(self) -> tuple[str, str]:
        """返回规范化后的 (主体, 属性) 槽位键，同一槽位的记忆互相比对冲突。"""
        return (normalize(self.subject), normalize(self.attribute))


@dataclass
class MemoryRecord:
    id: str
    user_id: str
    tenant_id: str | None
    chat_id: str | None
    scope: str
    kind: str
    subject: str
    attribute: str
    value_text: str
    confidence: float
    source: str
    evidence_ref: str | None
    importance: float
    valid_from: float
    valid_to: float | None
    created_at: float
    status: str
    supersedes_id: str | None = None
    disputed_with: str | None = None
    access_count: int = 0
    last_accessed: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def normalize(text: str) -> str:
    """规范化槽位键：转小写、合并空白、去除标点。"""
    cleaned = "".join(ch for ch in text.lower().strip() if ch.isalnum() or ch in " _-")
    return " ".join(cleaned.split())
