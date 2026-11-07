"""需认证的模型管理接口；服务商密钥不会离开本后端。"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic_ai import Agent, UsageLimits

from src.api.dependencies import require_admin
from src.midplatform import agent_models
from src.midplatform.tenant_access import Principal, get_principal

router = APIRouter(prefix="/workspace/models", tags=["agent-models"])


@router.get("")
def list_models(principal: Principal = Depends(get_principal)):
    """列出当前账号可用的模型；管理员看到全部模型及接口地址等管理字段，普通用户只看到分发给自己的已启用模型。"""
    return {
        "data": [
            agent_models.public_model(row, admin=principal.role == "admin")
            for row in agent_models.records()
            if principal.role == "admin" or agent_models.model_available(row, principal)
        ]
    }


@router.post("")
def add_model(body: agent_models.ModelForm, _: Principal = Depends(require_admin)):
    """管理员新增一个模型；密钥加密保存，响应不返回密钥，校验失败返回 400。"""
    try:
        return agent_models.save_model(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{identifier}")
def update_model(
    identifier: str, body: agent_models.ModelForm, _: Principal = Depends(require_admin)
):
    """管理员更新指定模型；更换厂商或接口地址时必须重新填写或明确清除旧密钥，否则返回 400。"""
    try:
        return agent_models.save_model(body, identifier)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{identifier}/default")
def default_model(identifier: str, _: Principal = Depends(require_admin)):
    """管理员把指定模型设为系统默认；该模型须存在、已启用且对全体账号可用，否则返回 400。"""
    try:
        agent_models.set_default(identifier)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{identifier}/test")
async def test_model(identifier: str, _: Principal = Depends(require_admin)):
    """管理员测试指定模型能否连通并成功发起工具调用（30 秒超时）；失败只返回概括信息，不泄露密钥或上游响应。"""
    try:
        record = agent_models.resolve_model(identifier, enabled_only=False)
        called = False

        def nexrag_connection_check(value: str) -> str:
            """Check that the model can invoke a NexRAG function with arguments."""
            nonlocal called
            called = value == "NEXRAG_MODEL_OK"
            return "NEXRAG_MODEL_OK"

        agent = Agent(agent_models.build_model(record), tools=[nexrag_connection_check], retries=0)
        await asyncio.wait_for(
            agent.run(
                "Call nexrag_connection_check with value NEXRAG_MODEL_OK, then briefly confirm success.",
                usage_limits=UsageLimits(request_limit=2),
            ),
            timeout=30,
        )
        if not called:
            raise HTTPException(422, "模型已响应，但未通过工具调用验证；请确认模型支持工具调用")
        return {"ok": True, "message": "连接与工具调用验证通过", "model_name": record["model_name"]}
    except HTTPException:
        raise
    except Exception as exc:
        # SDK 异常可能含凭据或响应体，不得向外转发。
        code = getattr(exc, "status_code", None)
        detail = f"（HTTP {code}）" if isinstance(code, int) else ""
        raise HTTPException(
            502, "模型测试失败" + detail + "，请检查接口、密钥、模型名称及工具调用支持"
        ) from None
