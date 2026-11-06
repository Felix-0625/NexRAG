"""单一 PydanticAI Agent，整合受治理的业务、文档、网页搜索、文件与记忆工具。

每次请求中，runner 解析当前生效的制品版本（提示词指引与调优参数，可能是灰度版本），
召回长期记忆，运行 Agent 并记录 trace，再在后台根据用户消息安排记忆抽取。
"""

from __future__ import annotations

import logging

from pydantic_ai import Agent, RunContext, ToolOutput
from pydantic_ai.toolsets import FunctionToolset

from src.agent.authorization import memory_helpers_allowed
from src.agent.context import build_prompt
from src.agent.core_tools import register_core_tools
from src.agent.dependencies import AgentDeps, UserContext  # noqa: F401 - 稳定的公开导入
from src.agent.execution import AgentExecution, drain_background  # noqa: F401 - 稳定的公开导入
from src.agent.model_hooks import build_model_hooks, validated_model_settings
from src.agent.output_validator import register_output_validator
from src.agent.prompts import SAFETY_PREFIX
from src.agent.tools import business_schema
from src.agent.workspace_tools import build_workspace_toolset
from src.config import settings
from src.evolution.artifacts import default_params
from src.evolution.routing import ActiveArtifacts, resolve
from src.memory.service import format_memories

log = logging.getLogger(__name__)


class AgenticRunner(AgentExecution):
    """复用同一个 PydanticAI Agent，同时为每次运行提供全新的授权信息。"""

    def __init__(self):
        self._agent: Agent[AgentDeps, str] | None = None

    def _build_agent(self, initial_model=None) -> Agent[AgentDeps, str]:
        from src.midplatform.agent_models import build_model, resolve_model

        model = initial_model if initial_model is not None else build_model(resolve_model())

        from src.agent.metric_results import BusinessReport

        # 安全前缀固定不变；指引与记忆块按次运行注入，使演进后的提示词版本无需重启即可生效。
        agent = Agent(
            model,
            system_prompt=SAFETY_PREFIX,
            deps_type=AgentDeps,
            output_type=[
                str,
                ToolOutput(BusinessReport, name="complete_business_report", strict=False),
            ],
            retries={"tools": 1, "output": 2},
            model_settings=validated_model_settings,
            capabilities=[build_model_hooks()],
        )
        from src.agent.metric_tools import register_metric_tools

        register_metric_tools(agent)
        from src.agent.evidence_arithmetic import register_arithmetic_tool

        register_arithmetic_tool(agent)
        from src.agent.indexed_return_tool import register_indexed_return_tool

        register_indexed_return_tool(agent)

        @agent.instructions
        def dynamic_instructions(ctx: RunContext[AgentDeps]) -> str:
            from src.agent.agent_instructions import build_dynamic_instructions

            return build_dynamic_instructions(
                ctx.deps, schema_builder=lambda: business_schema(settings.METADATA_PATH)
            )

        register_core_tools(agent)
        register_output_validator(agent)

        return agent

    @staticmethod
    def _full_prompt(prompt: str, history: list[tuple[str, str]] | None) -> str:
        return build_prompt(prompt, history)

    def _workspace_toolset(self, deps: AgentDeps) -> FunctionToolset:
        return build_workspace_toolset(deps)

    async def _prepare(
        self, prompt: str, context: UserContext
    ) -> tuple[AgentDeps, ActiveArtifacts]:
        """解析本次运行生效的制品版本，并召回长期记忆。"""
        try:
            active = resolve(context.user_id)
        except Exception:  # noqa: BLE001 - 回退到内置默认值
            log.exception("Artifact resolution failed; using defaults")
            active = ActiveArtifacts(prompt="", params=default_params())
        deps = AgentDeps(context=context, params=active.params, guidance=active.prompt or None)

        from src.memory.runtime import get_memory_service

        service = get_memory_service() if memory_helpers_allowed(context) else None
        if service is not None:
            try:
                recalled = await service.recall(prompt, context.memory_scope())
                deps.memory_block = format_memories(recalled)
                deps.memory_hits = len(recalled)
            except Exception:  # noqa: BLE001
                log.warning("Memory recall failed; continuing without memory", exc_info=True)
        return deps, active


runner = AgenticRunner()
