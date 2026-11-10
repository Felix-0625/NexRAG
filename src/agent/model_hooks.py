"""Model hooks for live authorization, verified-report rendering, and provider settings."""

from __future__ import annotations

from pydantic_ai.capabilities import Hooks

from src.agent.authorization import refresh_policy, require_business


async def validate_model_request(ctx, request_context):
    """Refresh grants and validate model, tenant, and data policy before normalizing report mode."""
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
    """Reauthorize and render completed certified calculations directly, otherwise delegate."""
    from pydantic_ai.messages import ModelResponse, TextPart

    from src.agent.indexed_returns import completed_comparisons
    from src.agent.metric_results import render_business_body, report_from_evidence
    from src.agent.temporal_report import temporal_results

    if temporal_results(ctx.deps.ledger) or completed_comparisons(ctx.deps.ledger):
        # The wrap hook can bypass before-request hooks, so repeat live authorization and output validation
        # rather than spend another model call transcribing certified values and units.
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
    """Choose model settings from this run's model record and missing evidence."""
    from src.agent.model_settings import agent_model_settings

    return agent_model_settings(ctx.deps.model_record or {}, ctx.deps.ledger.missing())


def build_model_hooks() -> Hooks:
    """Assemble model-request hook capabilities."""
    from src.agent.metric_results import prepare_business_output, prepare_business_tools

    return Hooks(
        model_request=finalize_verified_report,
        before_model_request=validate_model_request,
        prepare_tools=prepare_business_tools,
        prepare_output_tools=prepare_business_output,
    )
