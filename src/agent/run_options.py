"""针对同一条经过校验的生产 Agent 路径的内部实验控制项。

这些选项由受信任的评测代码构造，聊天 HTTP schema 不接受它们。实验会固定指引/参数，
并在内存中收集原始观测，不会把合成轮次写入用户记忆。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvaluationRun:
    guidance: str
    params: dict[str, Any]
    required_tools: list[str] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)
    denied: bool = False
    status: str = "ok"
    authorized_tenants: tuple[str, ...] = ()
    business_adapter: Any | None = None
    business_schema: str | None = None
    knowledge_documents: list[dict[str, Any]] | None = None
    business_metrics: dict[str, Any] | None = None
