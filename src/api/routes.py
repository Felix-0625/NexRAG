"""OpenAI-compatible agent chat and authenticated business-data endpoints."""

import json
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.agent.agent import UserContext, runner
from src.agent.grounding import GroundingFailure
from src.api.schemas import (
    BusinessQueryRequest,
    ChatCompletionRequest,
    ModelCard,
    ModelListResponse,
)
from src.config import settings
from src.midplatform import agent_models
from src.midplatform.adapters import create_business_adapter
from src.midplatform.data_policy import effective_policy
from src.midplatform.tenant_access import Principal, allowed_tenants, get_principal
from src.midplatform.workspace import get_setting

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/models", response_model=ModelListResponse)
async def list_models(request: Request):
    """Advertise the actual agent model rather than the NexRAG service alias."""
    principal = (
        get_principal(request) if request.headers.get(settings.NEXRAG_USER_JWT_HEADER) else None
    )
    return ModelListResponse(
        data=[
            ModelCard(
                id=row["id"], name=row["name"], is_default=row["id"] == agent_models.default_id()
            )
            for row in agent_models.records()
            if (
                agent_models.model_available(row, principal)
                if principal is not None
                else row["enabled"] and row.get("audience", "all") == "all"
            )
        ]
    )


def _authorize_chat(request: ChatCompletionRequest, principal: Principal, tenant_id: str | None):
    """Validate model, tenant, and data policy; return the model record and effective policy."""
    try:
        selected_model = agent_models.resolve_model(request.model)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        agent_models.require_model_access(selected_model, principal)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if tenant_id and tenant_id not in allowed_tenants(principal):
        raise HTTPException(status_code=403, detail="Tenant access denied")
    policy = effective_policy(
        principal.email, tenant_id or "", principal.knowledge_scopes, principal.knowledge_labels
    )
    try:
        policy.check_model(selected_model)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    return selected_model, policy


def _split_conversation(request: ChatCompletionRequest) -> tuple[str, list[tuple[str, str]]]:
    """Use the last user message as prompt and preceding user or assistant messages as history."""
    user_messages = [index for index, item in enumerate(request.messages) if item.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=422, detail="A user message is required")
    current_index = user_messages[-1]
    prompt = request.messages[current_index].content
    history = [
        (item.role, item.content)
        for item in request.messages[:current_index]
        if item.role in {"user", "assistant"}
    ]
    return prompt, history


def _build_context(
    request: ChatCompletionRequest,
    principal: Principal,
    selected_model: dict,
    policy,
    *,
    tenant_id: str | None,
    chat_id: str | None,
    message_id: str | None,
    capture_memory: bool,
    completion_id: str,
) -> UserContext:
    """Build this turn's user context from the authorized request."""
    return UserContext(
        user_id=principal.user_id,
        model_id=selected_model["id"],
        private_trace=True,
        capture_memory=capture_memory,
        enforce_live_authorization=True,
        data_policy=policy.to_dict(),
        email=principal.email,
        role=principal.role,
        session_version=principal.session_version,
        business_source_generation=get_setting("business_source_generation", 0),
        tenant_id=tenant_id or "",
        chat_id=(chat_id or "")[:128] or None,
        # Link feedback to this trace using the Open WebUI assistant message ID.
        trace_id=(message_id or "")[:128] or completion_id,
        knowledge_scopes=list(principal.knowledge_scopes),
        knowledge_labels=principal.knowledge_labels,
        workspace_instructions="\n\n".join(
            item.content for item in request.messages if item.role in {"system", "developer"}
        )[:80_000],
        workspace_tools=request.workspace_tools,
        workspace_tool_ticket=request.workspace_tool_ticket,
    )


async def _sse_chunks(
    request: ChatCompletionRequest,
    principal: Principal,
    prompt: str,
    history: list[tuple[str, str]],
    context: UserContext,
    completion_id: str,
    created: int,
):
    """Yield OpenAI SSE answer chunks and structured error chunks."""
    index = 0
    try:
        async for token in runner.run_stream(prompt, context, history):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": token}
                        if index == 0
                        else {"content": token},
                        "finish_reason": None,
                    }
                ],
            }
            index += 1
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    except GroundingFailure as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'grounding_error'}}, ensure_ascii=False)}\n\n"
        return
    except TimeoutError:
        err_chunk = {"error": {"message": "Agent response timed out", "type": "timeout"}}
        yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
        return
    except Exception as exc:
        log.exception("NexRAG Agent streaming failed for user %s", principal.user_id)
        err_chunk = {"error": {"message": str(exc), "type": "agent_error"}}
        yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
        return

    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request.model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        **({"usage": context.usage} if context.usage else {}),
    }
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _complete_once(
    request: ChatCompletionRequest,
    principal: Principal,
    prompt: str,
    history: list[tuple[str, str]],
    context: UserContext,
    completion_id: str,
    created: int,
) -> dict:
    """Run one nonstreaming turn and map failures to HTTP status codes."""
    try:
        answer = await runner.run(prompt, context, history)
    except GroundingFailure as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Agent response timed out") from exc
    except Exception as exc:
        log.exception("NexRAG Agent failed for user %s", principal.user_id)
        raise HTTPException(status_code=502, detail="Agent response failed") from exc

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": request.model,
        **({"usage": context.usage} if context.usage else {}),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    principal: Principal = Depends(get_principal),
    tenant_id: str | None = Header(default=None, alias="X-NexRAG-Tenant-Id"),
    chat_id: str | None = Header(default=None, alias="X-OpenWebUI-Chat-Id"),
    message_id: str | None = Header(default=None, alias="X-OpenWebUI-Message-Id"),
    capture_memory: bool = Header(default=True, alias="X-NexRAG-Capture-Memory"),
):
    """Run an authorized agent turn and format an OpenAI chat response."""
    selected_model, policy = _authorize_chat(request, principal, tenant_id)
    prompt, history = _split_conversation(request)
    completion_id = f"chatcmpl-{uuid4().hex}"
    context = _build_context(
        request,
        principal,
        selected_model,
        policy,
        tenant_id=tenant_id,
        chat_id=chat_id,
        message_id=message_id,
        capture_memory=capture_memory,
        completion_id=completion_id,
    )
    created = int(time.time())

    if request.stream:
        return StreamingResponse(
            _sse_chunks(request, principal, prompt, history, context, completion_id, created),
            media_type="text/event-stream",
        )
    return await _complete_once(
        request, principal, prompt, history, context, completion_id, created
    )


@router.get("/tenants")
def list_tenants(principal: Principal = Depends(get_principal)):
    """List business tenants granted to the authenticated Open WebUI user."""
    return {
        "data": [
            {"tenant_id": tenant_id, "label": label}
            for tenant_id, label in allowed_tenants(principal).items()
        ]
    }


@router.post("/business/query")
def business_query(
    body: BusinessQueryRequest,
    principal: Principal = Depends(get_principal),
):
    """Execute a bounded analytical query for an authorized tenant."""
    if body.tenant_id not in allowed_tenants(principal):
        raise HTTPException(status_code=403, detail="Tenant access denied")
    try:
        rows = create_business_adapter(settings.BUSINESS_DB_PATH).execute_query(
            body.sql,
            body.tenant_id,
            max_rows=body.max_rows,
            timeout_seconds=body.timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Business database is unavailable") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tenant_id": body.tenant_id, "row_count": len(rows), "rows": rows}
