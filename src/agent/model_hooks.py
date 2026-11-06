"""模型请求钩子：请求前的实时授权校验、已验证报告的直接渲染、模型参数。"""

from __future__ import annotations

from pydantic_ai.capabilities import Hooks

from src.agent.authorization import refresh_policy, require_business


async def validate_model_request(ctx, request_context):
    """每次模型请求前实时刷新授权：校验模型访问、业务租户授权（启用实时授权时）和数据策略，再规范化报告模式。"""
    from src.midplatform.agent_models import require_model_access, resolve_model

    policy = await refresh_policy(ctx.deps.context)
    if ctx.deps.context.enforce_live_authorization:
        require_model_access(resolve_model(ctx.deps.context.model_id), ctx.deps.context)
    if ctx.deps.context.enforce_live_authorization and ctx.deps.context.tenant_id:
        require_business(ctx.deps.context, ctx.deps.evaluation)
    policy.check_model(ctx.deps.model_record or resolve_model(ctx.deps.context.model_id))
    from src.agent.metric_results import normalize_report_mode

    return normalize_report_mode(request_context)


async def finalize_verified_report(ctx, *, request_context, handler):
    """期间计算或指数收益比较已完成时，复核授权后直接渲染已验证的报告并跳过模型调用；否则交给原处理器。"""
    from pydantic_ai.messages import ModelResponse, TextPart

    from src.agent.indexed_returns import completed_comparisons
    from src.agent.metric_results import render_business_body, report_from_evidence
    from src.agent.temporal_report import temporal_results

    if temporal_results(ctx.deps.ledger) or completed_comparisons(ctx.deps.ledger):
        # wrap 钩子会先于常规的 before-request 钩子短路；因此复用其实时授权检查，再走常规输出校验器，
        # 而不是再花一次模型调用把已认证的数值/单位抄进报告。
        await validate_model_request(ctx, request_context)
        output = render_business_body(report_from_evidence(ctx.deps.ledger), ctx.deps.ledger)
        ctx.deps.completion_mode = "verified_report_renderer"
        return ModelResponse(
            [TextPart(output)],
            model_name="nexrag:verified-report-renderer",
            metadata={"completion_mode": ctx.deps.completion_mode},
        )
    return await handler(request_context)


def validated_model_settings(ctx):
    """按本次运行的模型记录与账本中缺失的证据确定模型参数。"""
    from src.agent.model_settings import agent_model_settings

    return agent_model_settings(ctx.deps.model_record or {}, ctx.deps.ledger.missing())


def build_model_hooks() -> Hooks:
    """组装模型请求相关的 Hooks 能力。"""
    from src.agent.metric_results import prepare_business_output, prepare_business_tools

    return Hooks(
        model_request=finalize_verified_report,
        before_model_request=validate_model_request,
        prepare_tools=prepare_business_tools,
        prepare_output_tools=prepare_business_output,
    )
