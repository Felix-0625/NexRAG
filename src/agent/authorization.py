"""Recheck data and egress authorization immediately before each agent operation.

Signed request identity bounds scope; tool arguments cannot expand it. Reload policies during
long runs so revocation takes effect immediately. Synthetic evaluation grants stay internal,
never in the public chat schema.
"""

from __future__ import annotations

from src.midplatform.data_policy import effective_policy
from src.midplatform.tenant_access import Principal, allowed_tenants


def current_policy(context):
    """Compute current policy from the request's email, tenant, knowledge scopes, and tags."""
    return effective_policy(
        context.email, context.tenant_id, context.knowledge_scopes, context.knowledge_labels
    )


async def refresh_policy(context):
    """Reload knowledge authorization before recomputing policy so revocations take effect now."""
    from src.midplatform.data_policy import refresh_knowledge_authorization

    await refresh_knowledge_authorization(context)
    return current_policy(context)


def require_business(context, evaluation=None):
    """Require the request tenant to remain authorized; fail closed unless using internal synthetic
    grants.
    """
    if evaluation is not None:
        allowed = evaluation.authorized_tenants
    else:
        from fastapi import HTTPException

        try:
            allowed = allowed_tenants(Principal(context.user_id, context.email, context.role))
        except HTTPException as exc:
            raise ValueError("业务数据库授权策略不可用，拒绝查询") from exc
    if not context.tenant_id or context.tenant_id not in allowed:
        raise ValueError("业务数据库访问权限已撤销或未授权")


def memory_helpers_allowed(context) -> bool:
    """Keep private memory on-device even when helper models differ from the chat model."""
    from src.memory.local_policy import local_memory_configuration
    from src.memory.user_policy import memory_enabled

    return local_memory_configuration() and memory_enabled(context.user_id)
