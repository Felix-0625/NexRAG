"""核心内置工具：规划、业务库查询、知识库检索、联网搜索、文件检查、导出与记忆召回。"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import httpx
from pydantic_ai import ModelRetry, RunContext

from src.agent.authorization import (
    memory_helpers_allowed,
    refresh_policy,
    require_business,
)
from src.agent.context import bound_documents
from src.agent.dependencies import AgentDeps
from src.agent.exports import ExportSecurityError
from src.agent.exports import export_analysis as export_analysis_file
from src.agent.grounding import (
    requested_capabilities,
)
from src.agent.tools import (
    business_schema,
    inspect_temporary_file,
    query_business,
    register_plan,
    search_knowledge,
    search_web,
)
from src.config import settings

# 检索结果中保留的溯源元数据字段
_CITATION_METADATA_KEYS = frozenset(
    {"collection_name", "filename", "source", "section", "page", "version"}
)


def _can_reflect(ctx: RunContext[AgentDeps]) -> bool:
    """本次运行是否还有自我反思（重试提示）的额度。"""
    return ctx.deps.reflections < int(
        ctx.deps.params.get("max_tool_retries", settings.MAX_TOOL_RETRIES)
    )


async def plan_analysis(ctx: RunContext[AgentDeps], sub_tasks: list[str]) -> dict[str, Any]:
    """Register structured sub-tasks for multi-step or comparative analysis before gathering evidence."""
    steps, result = register_plan(sub_tasks)
    proposed = requested_capabilities("\n".join(steps), bool(ctx.deps.context.tenant_id))
    from src.agent.task_intent import plan_prohibition_conflicts

    conflicts = plan_prohibition_conflicts(ctx.deps.ledger.prompt, proposed)
    if conflicts:
        raise ModelRetry(
            "The proposed plan contradicts the user's explicit tool prohibitions: "
            + ", ".join(sorted(conflicts))
            + ". Revise the plan within the original request; no plan steps were registered."
        )
    ctx.deps.ledger.add("plan", {"step_count": len(steps)})
    ctx.deps.ledger.required |= proposed
    if steps:
        ctx.deps.plan_steps = steps
    return result


async def get_business_schema(
    _ctx: RunContext[AgentDeps],
    query: str | None = None,
    table_name: str | None = None,
) -> str:
    """List authorized business views, columns, and metric formulas with dynamic pruning."""
    if not _ctx.deps.context.tenant_id:
        return "当前账号没有业务数据访问上下文，请使用已授权知识库。"
    if _ctx.deps.evaluation and _ctx.deps.evaluation.business_schema:
        return _ctx.deps.evaluation.business_schema
    return business_schema(
        settings.METADATA_PATH,
        query=query,
        table_name=table_name,
        max_tables=int(_ctx.deps.params.get("prune_max_tables", 2)),
    )


async def _execute_business_query(ctx: RunContext[AgentDeps], sql: str) -> dict:
    """按评估适配器、业务会话或默认业务库的优先级执行只读查询。"""
    if ctx.deps.evaluation and ctx.deps.evaluation.business_adapter is not None:
        from src.agent.business_session import current_business_session

        session = current_business_session()
        adapter = ctx.deps.evaluation.business_adapter
        if session is not None and hasattr(adapter, "open_snapshot"):
            return await asyncio.to_thread(
                session.read, sql, ctx.deps.context.tenant_id, adapter=adapter
            )
        rows = await asyncio.to_thread(
            adapter.execute_query, sql, ctx.deps.context.tenant_id, max_rows=50
        )
        return {"row_count": len(rows), "rows": rows}
    return await asyncio.to_thread(
        query_business, sql, ctx.deps.context.tenant_id, settings.BUSINESS_DB_PATH
    )


def _empty_result_reflection(result: dict) -> dict:
    """查询返回 0 行时附带自我反思提示。"""
    return {
        **result,
        "row_count": 0,
        "rows": [],
        "reflection_hint": result.get("reflection_hint")
        or (
            "Query returned 0 rows. Self-reflection check: Did you use exact string matching "
            "that might need LOWER() or LIKE? Are date ranges or status filters too restrictive? "
            "Consider checking distinct column values or relaxing filters."
        ),
    }


def _query_failure_result(ctx: RunContext[AgentDeps], exc: Exception) -> dict:
    """SQL 执行失败时返回错误，额度内附带重试提示。"""
    if _can_reflect(ctx):
        ctx.deps.reflections += 1
        return {
            "error": str(exc),
            "reflection_hint": (
                "SQL execution failed. Please inspect the error, call get_business_schema "
                "if column names or table names need verification, and retry with corrected SQL."
            ),
        }
    return {"error": str(exc)}


async def query_database(ctx: RunContext[AgentDeps], sql: str) -> dict:
    """Run one read-only SQL query for the authorized business tenant."""
    try:
        require_business(ctx.deps.context, ctx.deps.evaluation)
        from src.agent.query_basis import require_query_filter_basis

        require_query_filter_basis(
            sql, ctx.deps.ledger.prompt, tenant_id=ctx.deps.context.tenant_id
        )
        result = await _execute_business_query(ctx, sql)
        rows = result.get("rows", [])
        result["evidence_id"] = ctx.deps.ledger.add(
            "db",
            rows,
            sql=sql,
            query_semantics=result.get("query_semantics"),
            snapshot_id=result.get("snapshot_id"),
        )
        if not rows and _can_reflect(ctx):
            ctx.deps.reflections += 1
            return _empty_result_reflection(result)
        ctx.deps.evidence_calls += 1
        return result
    except (
        ValueError,
        FileNotFoundError,
        ConnectionError,
        TimeoutError,
        sqlite3.Error,
    ) as exc:
        return _query_failure_result(ctx, exc)


async def _retrieve_documents(ctx: RunContext[AgentDeps], query: str) -> list[dict] | dict:
    """检查外发策略后取回文档；被拒绝时返回错误字典。"""
    from src.midplatform.data_policy import is_loopback_endpoint

    policy = await refresh_policy(ctx.deps.context)
    if not is_loopback_endpoint(settings.RETRIEVAL_API_BASE_URL) and not policy.external_models:
        return {"error": "当前数据禁止发送至外部检索服务"}
    if ctx.deps.evaluation is not None:
        if ctx.deps.evaluation.knowledge_documents is None:
            return {
                "error": "No isolated evaluation knowledge fixture was supplied; live knowledge is not queried"
            }
        return [dict(document) for document in ctx.deps.evaluation.knowledge_documents]
    return await search_knowledge(
        query,
        base_url=settings.RETRIEVAL_API_BASE_URL,
        api_key=settings.RETRIEVAL_API_KEY or "",
        collection_name=ctx.deps.context.knowledge_scopes,
        top_k=max(1, min(3, int(ctx.deps.params.get("knowledge_top_k", 3)))),
    )


def _bound_new_documents(ctx: RunContext[AgentDeps], documents: list[dict]) -> list[dict]:
    """剔除本轮已展示的文档，并按剩余上下文预算截取，同时记录已用上下文。"""
    unique_documents = []
    import hashlib

    for document in documents:
        fingerprint = hashlib.sha256(str(document.get("content", "")).encode()).hexdigest()
        if fingerprint not in ctx.deps.document_context_keys:
            unique_documents.append({**document, "_context_key": fingerprint})
    documents = bound_documents(
        unique_documents, char_budget=max(0, 12_000 - ctx.deps.document_context_chars)
    )
    for document in documents:
        ctx.deps.document_context_keys.add(document.pop("_context_key"))
        ctx.deps.document_context_chars += len(str(document.get("content", "")))
    return documents


def _citation_metadata(documents: list[dict]) -> list[dict]:
    # 保留引用所需的溯源信息，避免每轮检索都重复携带体积较大的索引哈希/所有者元数据。
    return [
        {
            **doc,
            "metadata": {
                key: value
                for key, value in doc.get("metadata", {}).items()
                if key in _CITATION_METADATA_KEYS
            },
        }
        for doc in documents
    ]


def _knowledge_response(
    ctx: RunContext[AgentDeps], documents: list[dict], returned_count: int, evidence_id: str
) -> dict:
    """按预算耗尽、无结果、低相关度的顺序给出提示，否则返回正常结果。"""
    if not documents and returned_count:
        return {
            "evidence_id": evidence_id,
            "documents": [],
            "context_budget_omitted": returned_count,
            "context_warning": "Matching parents were already shown or exceed the remaining context budget. "
            "Use the previously returned evidence. Do not treat this as no matching documents.",
        }
    if not documents and _can_reflect(ctx):
        ctx.deps.reflections += 1
        return {
            "evidence_id": evidence_id,
            "documents": [],
            "reflection_hint": (
                "No relevant documents found. Self-reflection check: Try rewriting the query "
                "using broader keywords, English/domain synonyms, or removing overly specific qualifiers."
            ),
        }
    min_relevance = float(
        ctx.deps.params.get("retrieval_min_relevance", settings.RETRIEVAL_MIN_RELEVANCE)
    )
    if documents:
        top_score = max((doc.get("score") or 0.0) for doc in documents)
        if top_score < min_relevance and _can_reflect(ctx):
            ctx.deps.reflections += 1
            return {
                "evidence_id": evidence_id,
                "documents": documents,
                "low_relevance_warning": (
                    f"Top document score ({top_score:.3f}) is below confidence threshold "
                    f"({min_relevance}). Consider rewriting the query keywords "
                    "to retrieve more focused context."
                ),
            }
    ctx.deps.evidence_calls += 1
    return {
        "evidence_id": evidence_id,
        "documents": documents,
        "context_budget_omitted": returned_count - len(documents),
    }


async def search_knowledge_documents(ctx: RunContext[AgentDeps], query: str) -> dict:
    """Internal retrieval of personal/granted knowledge and verified attachments.

    This is not an external/MCP tool and requires no business database.
    Use it for policy/document questions even when external tools are prohibited.
    """
    try:
        documents = await _retrieve_documents(ctx, query)
        if isinstance(documents, dict):
            return documents
        returned_count = len(documents)
        documents = _citation_metadata(_bound_new_documents(ctx, documents))
        evidence_id = ctx.deps.ledger.add(
            "kb", documents, omitted_count=returned_count - len(documents)
        )
        return _knowledge_response(ctx, documents, returned_count, evidence_id)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return {"error": f"Knowledge retrieval unavailable: {exc}"}


async def search_web_engine(ctx: RunContext[AgentDeps], query: str) -> dict[str, Any]:
    """Search external web information when local business DB or document knowledge is insufficient."""
    if not settings.ENABLE_WEB_SEARCH:
        return {"error": "External web search is disabled by configuration."}
    try:
        policy = await refresh_policy(ctx.deps.context)
        if ctx.deps.context.enforce_live_authorization and ctx.deps.context.tenant_id:
            require_business(ctx.deps.context, ctx.deps.evaluation)
    except ValueError:
        return {"error": "账号或数据授权已变更，未发送搜索内容，请重新开始对话"}
    if not policy.external_search:
        return {"error": "当前数据范围禁止对外搜索，未发送查询内容"}
    from src.midplatform.search_configuration import search_configuration

    configuration = search_configuration()
    if configuration["status"] != "configured":
        return {"error": configuration["message"]}
    try:
        results = await search_web(
            query,
            searxng_url=configuration["endpoint"],
            timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS,
        )
        if results and results[0].get("engine") != "fallback":
            ctx.deps.ledger.add("web", results)
            ctx.deps.evidence_calls += 1
        return {"web_results": results}
    except Exception as exc:
        return {"error": f"Web search failed: {exc}"}


def inspect_local_file(
    ctx: RunContext[AgentDeps],
    file_path: str,
    max_rows: int = 10,
    start_line: int = 1,
    max_lines: int = 50,
) -> dict[str, Any]:
    """Inspect and sample a safe local or temporary file (CSV, TSV, JSON, TXT, MD, Log).

    Allows exploring column schemas, sample records, or text lines without
    polluting the persistent Qdrant knowledge collection.
    """
    if ctx.deps.evaluation is not None:
        return {"error": "Live file access is disabled in isolated synthetic evaluation"}
    result = inspect_temporary_file(
        file_path,
        max_rows=max_rows,
        start_line=start_line,
        max_lines=max_lines,
        user_id=ctx.deps.context.user_id,
    )
    if "error" not in result:
        ctx.deps.ledger.add("file", result)
        ctx.deps.evidence_calls += 1
    return result


def export_analysis(
    ctx: RunContext[AgentDeps],
    title: str,
    format: str = "md",
    rows: list[dict[str, Any]] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Save a finished analysis report or table to the user's export folder.

    Use this when the user asks to export, download, or save a result as a
    file. ``format`` is one of csv, json, md, txt. ``rows`` is a list of
    flat dict records (for csv/json/a Markdown table); ``text`` is free-form
    prose (for md/txt, or extra notes alongside rows). The file is written
    under the folder the deployer bound for this user; it is not used as
    evidence by itself and must follow an evidence-grounded answer.
    For knowledge summaries or user-provided prose, pass the Markdown
    body as ``text`` and leave ``rows`` unset. ``rows`` is reserved for
    exact structured records actually returned by query or data tools.
    """
    if ctx.deps.evaluation is not None:
        return {
            "error": "Live export writes are disabled in isolated evaluation; verify export through the frontend"
        }
    try:
        if issues := ctx.deps.ledger.export_issues(rows, text):
            raise ModelRetry("Export failed evidence checks: " + "; ".join(issues))
        result = export_analysis_file(
            user_id=ctx.deps.context.user_id,
            chat_id=ctx.deps.context.chat_id,
            title=title,
            fmt=format,
            rows=rows,
            text=text,
        )
        ctx.deps.exports.append(result)
        ctx.deps.ledger.add("export", {k: v for k, v in result.items() if k != "path"})
        return {key: value for key, value in result.items() if key != "path"}
    except (ValueError, ExportSecurityError) as exc:
        return {"error": str(exc)}


async def recall_memory(ctx: RunContext[AgentDeps], query: str) -> dict[str, Any]:
    """Search this user's long-term memory (preferences, earlier decisions, task context).

    Visibility (user, tenant, chat) is fixed by the server; memories are
    context about the user, not proof of business numbers.
    """
    from src.memory.runtime import get_memory_service

    if not memory_helpers_allowed(ctx.deps.context):
        return {"error": "当前数据的记忆处理仅允许本机服务，未向外部发送内容"}
    service = get_memory_service()
    if service is None:
        return {"memories": [], "note": "Long-term memory is disabled."}
    try:
        recalled = await service.recall(query, ctx.deps.context.memory_scope())
    except Exception as exc:  # noqa: BLE001 - 记忆故障不得中断本次运行
        return {"error": f"Memory recall unavailable: {exc}"}
    ctx.deps.memory_hits += len(recalled)
    return {
        "memories": [
            {
                "id": item.record.id,
                "scope": item.record.scope,
                "subject": item.record.subject,
                "attribute": item.record.attribute,
                "value": item.record.value_text,
                "source": item.record.source,
                "status": item.record.status,
            }
            for item in recalled
        ]
    }


def register_core_tools(agent) -> None:
    """按固定顺序注册核心工具（顺序与工具定义对 LLM 可见）。"""
    for tool in (
        plan_analysis,
        get_business_schema,
        query_database,
        search_knowledge_documents,
        search_web_engine,
        inspect_local_file,
        export_analysis,
        recall_memory,
    ):
        agent.tool(strict=False)(tool)
