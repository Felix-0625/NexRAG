"""OpenAI-compatible chat and business-query request models."""

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """One conversation message containing a role and text."""

    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    """Request body compatible with OpenAI /v1/chat/completions."""

    model: str = "NexRAG"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = 0.7
    workspace_tool_ticket: str | None = Field(default=None, max_length=128)
    workspace_tools: list[dict[str, Any]] = Field(default_factory=list, max_length=30)


class ModelCard(BaseModel):
    """One model descriptor in OpenAI model discovery."""

    id: str
    name: str | None = None
    nexrag_agent: bool = True
    is_default: bool = False
    info: dict[str, Any] = Field(
        default_factory=lambda: {
            "meta": {
                "description": "使用 NexRAG 的数据库、知识库与授权工具进行分析。",
                "capabilities": {
                    "builtin_tools": False,
                    "vision": False,
                    "web_search": False,
                    "image_generation": False,
                    "code_interpreter": False,
                    "terminal": False,
                    "memory": False,
                    "file_context": True,
                    "file_upload": True,
                    "citations": True,
                    "status_updates": True,
                },
            }
        }
    )
    object: str = "model"
    owned_by: str = "nexrag"


class ModelListResponse(BaseModel):
    """Response containing available models."""

    object: str = "list"
    data: List[ModelCard]


class BusinessQueryRequest(BaseModel):
    """Direct tenant-scoped read-only SQL analysis request."""

    tenant_id: str = Field(min_length=1)
    sql: str = Field(min_length=1, max_length=20_000)
    max_rows: int = Field(default=100, ge=1, le=1_000)
    timeout_seconds: float = Field(default=2.0, gt=0, le=10.0)
