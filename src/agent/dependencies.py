"""经过校验的单次请求身份，以及每次运行可变的 Agent 依赖。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from src.agent.grounding import EvidenceLedger
from src.agent.run_options import EvaluationRun
from src.evolution.artifacts import default_params
from src.memory.models import MemoryScope


class UserContext(BaseModel):
    model_id: str | None = None
    """Server-verified user and tenant selection for one chat request."""

    user_id: str
    email: str
    tenant_id: str
    chat_id: str | None = None
    trace_id: str | None = None
    knowledge_scopes: list[str] = Field(default_factory=list)
    workspace_instructions: str = ""
    workspace_tool_ticket: str | None = None
    workspace_tools: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict, exclude=True)
    private_trace: bool = True
    capture_memory: bool = True
    """Only interactive user turns are candidates for personal memory writes."""
    role: str = "user"
    data_policy: dict[str, Any] = Field(default_factory=dict)
    knowledge_labels: dict[str, dict[str, Any]] = Field(default_factory=dict)
    enforce_live_authorization: bool = False
    session_version: int | None = None
    business_source_generation: int | None = None

    def memory_scope(self) -> MemoryScope:
        return MemoryScope(user_id=self.user_id, tenant_id=self.tenant_id, chat_id=self.chat_id)


@dataclass
class AgentDeps:
    """每次运行的工具依赖；租户身份不能被 LLM 更改。"""

    context: UserContext
    evidence_calls: int = 0
    reflections: int = 0
    plan_steps: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=default_params)
    guidance: str | None = None
    memory_block: str = ""
    memory_hits: int = 0
    exports: list[dict[str, Any]] = field(default_factory=list)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    grounding_fallback: bool = False
    completion_mode: str | None = None
    evaluation: EvaluationRun | None = None
    model_record: dict[str, Any] | None = None
    document_context_chars: int = 0
    document_context_keys: set[str] = field(default_factory=set)
