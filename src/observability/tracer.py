"""将 Agent 运行记录为 trace、observation 和规则分数，存入 ops store。

数据模型沿用 Langfuse（trace -> observations -> scores），但存储在本地，
使评测和自我演进可完全离线运行。observation 在运行结束后由 PydanticAI 的
消息历史重建，因此无需 OpenTelemetry exporter。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from src.config import settings
from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now
from src.observability.redaction import hash_user, store_content

log = logging.getLogger(__name__)

TOOL_TYPES = {
    "query_database": "tool",
    "search_knowledge_documents": "retrieval",
    "search_web_engine": "retrieval",
    "inspect_local_file": "tool",
    "plan_analysis": "tool",
    "get_business_schema": "tool",
    "recall_memory": "memory",
}


@dataclass
class TraceRecord:
    """持久化之前收集的一次 Agent 运行的事实信息。"""

    trace_id: str
    user_id: str
    tenant_id: str | None
    chat_id: str | None
    input_text: str
    artifact_versions: dict[str, str] = field(default_factory=dict)
    canary: bool = False
    started_at: float = field(default_factory=now)


def _summarize(value: Any, limit: int = 600) -> str:
    """工具参数或返回值的紧凑、已脱敏的类 JSON 摘要。"""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return store_content(text, limit) or ""


def _tool_error(content: Any) -> str | None:
    """检测在"成功"的工具返回内部报告的工具级失败。"""
    if isinstance(content, dict):
        if content.get("error"):
            return str(content["error"])[:300]
        if content.get("reflection_hint"):
            return "reflection: " + str(content["reflection_hint"])[:200]
    return None


def extract_observations(messages: Iterable[ModelMessage]) -> list[dict[str, Any]]:
    """将工具调用与其返回/重试配对，生成有序的 observation。"""
    calls: dict[str, ToolCallPart] = {}
    observations: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    calls[part.tool_call_id] = part
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_name:
                    call = calls.get(part.tool_call_id)
                    # 当前版本的 PydanticAI 把工具的 ModelRetry 序列化为带类型化
                    # outcome 的 ToolReturnPart。不要根据任意文档/工具文本前缀
                    # 推断失败。
                    outcome = part.outcome if isinstance(part, ToolReturnPart) else "retried"
                    content = part.content
                    observations.append(
                        {
                            "type": TOOL_TYPES.get(part.tool_name, "tool"),
                            "name": part.tool_name,
                            "input_summary": _summarize(call.args if call else None),
                            "output_summary": _summarize(content),
                            "error": (
                                "retry: " + _summarize(content, 300)
                                if outcome == "retried"
                                else (
                                    f"tool outcome: {outcome}"
                                    if outcome in {"failed", "denied", "interrupted"}
                                    else _tool_error(content)
                                )
                            ),
                        }
                    )
    return observations


def _insert_trace(conn, trace, private, output_text, latency_ms, status, error) -> None:
    """写入 trace 行；私有 trace 不保存输入、输出和错误原文。"""
    conn.execute(
        "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trace.trace_id,
            hash_user(trace.user_id),
            trace.tenant_id,
            trace.chat_id,
            dumps(trace.artifact_versions),
            int(trace.canary),
            None if private else store_content(trace.input_text),
            None if private else store_content(output_text),
            latency_ms,
            status,
            ("execution_error" if error else None) if private else ((error or "")[:500] or None),
            trace.started_at,
        ),
    )


def _insert_observations(conn, trace_id, observations, private) -> None:
    """按顺序写入 observation 行；私有 trace 不保存摘要，只保留错误标记。"""
    for position, obs in enumerate(observations):
        conn.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                trace_id,
                obs["type"],
                obs["name"],
                None if private else obs["input_summary"],
                None if private else obs["output_summary"],
                ("tool_execution_error" if obs["error"] else None) if private else obs["error"],
                position,
            ),
        )


def _insert_scores(conn, trace_id, scores) -> None:
    """写入规则分数行。"""
    for name, value in scores.items():
        conn.execute(
            "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), trace_id, name, float(value), "numeric", "rule", None, now()),
        )


class Tracer:
    """持久化 trace 和分数；失败绝不影响用户请求。"""

    def __init__(self, store: OpsStore | None = None):
        self._store = store

    @property
    def store(self) -> OpsStore:
        return self._store or get_ops_store()

    def record(
        self,
        trace: TraceRecord,
        *,
        output_text: str | None,
        messages: Iterable[ModelMessage] = (),
        status: str = "ok",
        error: str | None = None,
        rule_scores: dict[str, float] | None = None,
    ) -> str | None:
        """写入一次已结束的运行；返回 trace ID，禁用或失败时返回 None。"""
        if not settings.ENABLE_TRACING:
            return None
        try:
            observations = extract_observations(messages)
            private = trace.artifact_versions.get("content_scope") == "private"
            latency_ms = int((now() - trace.started_at) * 1000)
            with self.store.transaction() as conn:
                _insert_trace(conn, trace, private, output_text, latency_ms, status, error)
                _insert_observations(conn, trace.trace_id, observations, private)
                scores = dict(rule_scores or {})
                scores["latency_ms"] = float(latency_ms)
                scores["tool_error_count"] = float(sum(1 for o in observations if o["error"]))
                scores["run_success"] = 1.0 if status == "ok" else 0.0
                _insert_scores(conn, trace.trace_id, scores)
            return trace.trace_id
        except Exception:  # noqa: BLE001 - 可观测性绝不能让请求失败
            log.exception("Failed to record trace %s", trace.trace_id)
            return None

    def add_score(
        self,
        trace_id: str,
        name: str,
        value: float,
        *,
        source: str,
        data_type: str = "numeric",
        comment: str | None = None,
    ) -> None:
        """为已有 trace 附加一个分数（用户反馈、judge、人工）。"""
        self.store.execute(
            "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), trace_id, name, float(value), data_type, source, comment, now()),
        )

    def purge_expired(self) -> int:
        """删除超过 TRACE_RETENTION_DAYS 的 trace；数据集副本保留。"""
        cutoff = now() - settings.TRACE_RETENTION_DAYS * 86_400
        rows = self.store.query("SELECT COUNT(*) AS n FROM traces WHERE created_at < ?", (cutoff,))
        self.store.execute("DELETE FROM traces WHERE created_at < ?", (cutoff,))
        return int(rows[0]["n"]) if rows else 0


tracer = Tracer()
