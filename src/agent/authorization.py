"""在 Agent 每次操作前即时重新检查数据/外发授权。

范围由签名的请求身份决定，工具参数无法扩大它。长时间运行期间会重新读取策略，
使撤销授权不必等到下一轮对话才生效。评测用的合成授权只存在于受信任的内部执行选项中，绝不出现在公开的聊天 schema 里。
"""

from __future__ import annotations

from src.midplatform.data_policy import effective_policy
from src.midplatform.tenant_access import Principal, allowed_tenants


def current_policy(context):
    """按请求身份中的邮箱、租户、知识范围和标签计算当前生效的数据策略。"""
    return effective_policy(
        context.email, context.tenant_id, context.knowledge_scopes, context.knowledge_labels
    )


async def refresh_policy(context):
    """先重新读取知识库授权，再返回最新的生效数据策略，使撤销授权立即生效。"""
    from src.midplatform.data_policy import refresh_knowledge_authorization

    await refresh_knowledge_authorization(context)
    return current_policy(context)


def require_business(context, evaluation=None):
    """确认请求身份的租户仍在授权范围内，否则抛出 ValueError；授权策略不可用时同样拒绝（评测运行使用其合成授权）。"""
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
    """私有记忆不得经由辅助 LLM/嵌入模型离开本机。"""
    from src.memory.local_policy import local_memory_configuration
    from src.memory.user_policy import memory_enabled

    return local_memory_configuration() and memory_enabled(context.user_id)
