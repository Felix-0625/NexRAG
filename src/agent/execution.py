"""Validated single-agent execution, tracing, and private-memory lifecycle.

HTTP chat and internal evaluation share one path; overrides only pin evaluated artifacts
and suppress user-memory and production-trace side effects.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from pydantic_ai import UsageLimits, capture_run_messages
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import RunUsage

from src.agent.authorization import memory_helpers_allowed, refresh_policy
from src.agent.dependencies import AgentDeps, UserContext
from src.agent.grounding import (
    EvidenceLedger,
    GroundingFailure,
    database_write_request,
    requested_capabilities,
)
from src.agent.run_options import EvaluationRun
from src.config import settings
from src.evolution.routing import ActiveArtifacts
from src.midplatform.ops_store import new_id
from src.observability.tracer import TraceRecord, tracer

log = logging.getLogger(__name__)
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """Keep a strong reference to a background coroutine until completion."""
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def drain_background(timeout: float = 5.0) -> None:
    """Wait for pending background tasks during tests or graceful shutdown."""
    if _background:
        await asyncio.wait(list(_background), timeout=timeout)


class _RunFinisher:
    """Finalize one run; evaluations update messages and status without production side effects."""

    def __init__(
        self,
        owner: AgentExecution,
        trace: TraceRecord,
        deps: AgentDeps,
        prompt: str,
        evaluation: EvaluationRun | None,
    ) -> None:
        self.owner = owner
        self.trace = trace
        self.deps = deps
        self.prompt = prompt
        self.evaluation = evaluation
        # The caller attaches the read-only session only after entering the turn's session context.
        self.business_session = None

    def __call__(self, **kwargs) -> None:
        # Retain an opaque consistency-source ID even when private traces redact SQL values and rows.
        session = self.business_session
        if session is not None and session.snapshot is not None:
            self.trace.artifact_versions["business_snapshot"] = session.snapshot.id
        if self.evaluation is not None:
            self.evaluation.messages[:] = kwargs.get("messages", [])
            self.evaluation.status = kwargs.get("status", "error")
        else:
            self.owner._finish(self.trace, self.deps, prompt=self.prompt, **kwargs)


def _pre_run_refusal(deps: AgentDeps, policy, prompt: str) -> tuple[str, str] | None:
    """Check scope, tool, and web grants before the model; return refusal text and status if
    needed.
    """
    from src.agent.business_access import explicit_scope_refusal

    if refusal := explicit_scope_refusal(deps):
        return refusal["message"], "denied"

    from src.agent.builtin_tools import DISCOUNT_TOOL_ID
    from src.agent.derived_calculation import CALCULATION

    if CALCULATION in deps.ledger.required and DISCOUNT_TOOL_ID not in policy.allowed_tool_ids:
        message = (
            "当前账号未获授权使用折扣报价计算工具，本次请求已拒绝，未执行计算。请联系管理员授权。"
        )
        return message, "denied"

    from src.agent.mcp_tools import explicit_tool_refusal

    if message := explicit_tool_refusal(prompt, policy):
        return message, "denied"

    if "web" in deps.ledger.required:
        from src.midplatform.search_configuration import search_configuration

        if not policy.external_search:
            return "当前账号或数据范围未获联网搜索授权，本次未发送搜索内容。", "denied"
        configuration = search_configuration()
        if configuration["status"] != "configured":
            return configuration["message"], "unavailable"
    return None


def _usage_snapshot(usage: RunUsage) -> dict[str, int]:
    """Convert PydanticAI usage into an OpenAI-style usage dictionary."""
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
    }


class AgentExecution:
    """Execution methods shared by AgenticRunner."""

    def _finish(
        self,
        trace: TraceRecord,
        deps: AgentDeps,
        *,
        prompt: str,
        output: str | None,
        messages,
        status: str,
        error: str | None = None,
    ) -> None:
        """Record the trace and schedule memory extraction from the user's words."""
        if trace.chat_id and trace.chat_id.startswith("temporary:"):
            # Temporary chats must skip both trace storage and background long-term memory extraction.
            return
        if deps.completion_mode:
            trace.artifact_versions["completion_mode"] = deps.completion_mode
        tracer.record(
            trace,
            output_text=output,
            messages=messages,
            status=status,
            error=error,
            rule_scores={
                "evidence_calls": float(deps.evidence_calls),
                "reflections": float(deps.reflections),
                "grounding_rejections": float(deps.ledger.rejections),
                "grounding_fallback": float(deps.grounding_fallback),
                "task_completed": float(status == "ok" and not deps.ledger.missing()),
                "plan_steps": float(len(deps.plan_steps)),
                "memory_hits": float(deps.memory_hits),
            },
        )
        if deps.plan_steps:
            log.info("Agent run used plan_analysis with %s steps", len(deps.plan_steps))
        if status != "ok" or not deps.context.capture_memory:
            return
        from src.memory.runtime import get_memory_service

        service = get_memory_service() if memory_helpers_allowed(deps.context) else None
        if service is not None:
            _spawn(self._ingest(service, prompt, deps.context, trace.trace_id))

    @staticmethod
    async def _ingest(service, prompt: str, context: UserContext, trace_id: str) -> None:
        try:
            if not context.capture_memory or not memory_helpers_allowed(context):
                return
            await service.ingest_turn(prompt, context.memory_scope(), evidence_ref=trace_id)
        except Exception:  # noqa: BLE001 - Background work is best effort.
            log.warning("Memory ingestion failed for trace %s", trace_id, exc_info=True)

    def _trace(self, prompt: str, context: UserContext, active: ActiveArtifacts) -> TraceRecord:
        return TraceRecord(
            trace_id=context.trace_id or new_id(),
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            chat_id=context.chat_id,
            input_text=prompt,
            artifact_versions={
                **active.versions,
                **({"model": context.model_id} if context.model_id else {}),
                **({"content_scope": "private"} if context.private_trace else {}),
                **({"memory_capture": "disabled"} if not context.capture_memory else {}),
            },
            canary=active.canary,
        )

    async def run(
        self,
        prompt: str,
        context: UserContext,
        history: list[tuple[str, str]] | None = None,
        *,
        evaluation: EvaluationRun | None = None,
    ) -> str:
        """Let the agent select tools and produce an evidence-grounded answer."""
        if database_write_request(prompt):
            if evaluation is not None:
                evaluation.denied = True
            return (
                "业务数据库仅支持只读查询。本次请求涉及写入或删除，已拒绝执行，没有修改业务数据。"
            )
        selected_model, policy, model_args = await self._authorize_model(context)
        deps, active = await self._start_deps(prompt, context, evaluation)
        self._bind_requirements(deps, prompt, context, policy, evaluation, selected_model)
        trace = self._trace(prompt, context, active)
        trace.artifact_versions["model"] = selected_model["id"]
        finish = _RunFinisher(self, trace, deps, prompt, evaluation)
        if refusal := _pre_run_refusal(deps, policy, prompt):
            message, status = refusal
            finish(output=message, messages=[], status=status)
            return message
        return await self._invoke_and_record(prompt, history, context, deps, model_args, finish)

    async def _authorize_model(self, context: UserContext):
        """Resolve the model, refresh and validate grants, and ensure the agent is built."""
        from src.midplatform.agent_models import build_model, resolve_model

        selected_model = resolve_model(context.model_id)
        policy = await refresh_policy(context)
        if context.enforce_live_authorization:
            from src.midplatform.agent_models import require_model_access

            require_model_access(selected_model, context)
        policy.check_model(selected_model)
        active_model = build_model(selected_model)
        if self._agent is None:
            self._agent = self._build_agent(initial_model=active_model)
        # Resolve the current default each run so provider changes cannot reuse a stale cached agent.
        return selected_model, policy, {"model": active_model}

    async def _start_deps(
        self, prompt: str, context: UserContext, evaluation: EvaluationRun | None
    ):
        """Create ordinary run dependencies or pin artifacts for evaluation."""
        if evaluation is None:
            deps, active = await self._prepare(prompt, context)
        else:
            active = ActiveArtifacts(prompt=evaluation.guidance, params=evaluation.params)
            deps = AgentDeps(
                context=context,
                params=evaluation.params,
                guidance=evaluation.guidance,
                evaluation=evaluation,
            )
        return deps, active

    @staticmethod
    def _bind_requirements(
        deps: AgentDeps,
        prompt: str,
        context: UserContext,
        policy,
        evaluation: EvaluationRun | None,
        selected_model: dict,
    ) -> None:
        """Initialize the ledger with this turn's required capabilities, tools, and catalog
        obligations.
        """
        deps.ledger = EvidenceLedger(
            required=requested_capabilities(prompt, bool(context.tenant_id)), prompt=prompt
        )
        from src.agent.mcp_tools import requested_tool_ids

        # A named MCP request requires a successful call to that exact authorized tool in this turn.
        deps.ledger.required |= {
            "tool:" + tool_id for tool_id in requested_tool_ids(prompt, policy)
        }
        from src.agent.business_contract import bind_catalog_requirements
        from src.agent.task_intent import prohibited_tools

        if (
            settings.REQUIRE_KNOWLEDGE_EVIDENCE
            and context.knowledge_scopes
            and not context.tenant_id
            and not deps.ledger.required
            and "search_knowledge_documents" not in prohibited_tools(prompt)
        ):
            deps.ledger.required.add("kb")
        bind_catalog_requirements(deps.ledger, evaluation)
        deps.model_record = selected_model
        if evaluation is not None:
            required = {
                "query_database": "db",
                "query_business_metrics": "db",
                "search_knowledge_documents": "kb",
                "export_analysis": "export",
            }
            deps.ledger.required |= {
                required[name] for name in evaluation.required_tools if name in required
            }

    async def _invoke_and_record(
        self,
        prompt: str,
        history: list[tuple[str, str]] | None,
        context: UserContext,
        deps: AgentDeps,
        model_args: dict,
        finish: _RunFinisher,
    ) -> str:
        """Run the agent with a timeout, record usage and traces, and handle failures and fallback
        output.
        """
        usage = RunUsage()
        captured = []
        from src.agent.business_session import business_read_session

        try:
            async with business_read_session() as business_session:
                finish.business_session = business_session
                with capture_run_messages() as captured:
                    result = await asyncio.wait_for(
                        self._agent.run(
                            self._full_prompt(prompt, history),
                            deps=deps,
                            toolsets=[self._workspace_toolset(deps)],
                            usage_limits=UsageLimits(request_limit=10),
                            usage=usage,
                            **model_args,
                        ),
                        timeout=settings.AGENT_TIMEOUT_SECONDS,
                    )
        except (UnexpectedModelBehavior, UsageLimitExceeded, TimeoutError) as exc:
            context.usage = _usage_snapshot(usage)
            answer = deps.ledger.safe_answer()
            deps.grounding_fallback = True
            finish(
                output=answer,
                messages=captured,
                status="partial" if deps.ledger.entries else "error",
                error=type(exc).__name__,
            )
            if deps.ledger.missing() or not deps.ledger.entries:
                raise GroundingFailure(answer) from exc
            return answer
        except Exception as exc:
            status = "timeout" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "error"
            finish(output=None, messages=captured, status=status, error=str(exc))
            raise
        context.usage = _usage_snapshot(result.usage)
        from src.agent.metric_results import render_metric_results
        from src.agent.report_evidence import append_report_sources

        output = append_report_sources(result.output, deps.ledger) + render_metric_results(
            deps.ledger.entries
        )
        finish(
            output=output,
            messages=result.all_messages(),
            status="denied"
            if any(e["kind"] == "business_denial" for e in deps.ledger.entries)
            else "ok",
        )
        return output

    async def run_stream(
        self,
        prompt: str,
        context: UserContext,
        history: list[tuple[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        """Stream only validated output through OpenAI SSE.

        PydanticAI run_stream cannot retry output validators and may expose unverified text
        before
        refusal. Ordinary run supports retries, an overall timeout, and normalized export links.
        """
        yield await self.run(prompt, context, history)
