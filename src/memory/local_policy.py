"""个人记忆辅助组件仅限本地运行，与聊天模型的审批状态无关。"""

from __future__ import annotations

from src.config import settings
from src.midplatform.data_policy import is_loopback_endpoint, model_is_local


def cloud_model_name(name: str) -> bool:
    """模型名带 ":cloud" 或 "-cloud" 标记时视为云端路由模型。"""
    value = name.lower()
    return ":cloud" in value or value.endswith("-cloud")


def local_memory_configuration() -> bool:
    """个人记忆所需组件是否全部在本机：辅助 LLM、embedding 模型与端点、向量库均须为本地回环且非云端模型。"""
    return (
        model_is_local(
            {
                "provider": "ollama",
                "base_url": settings.LLM_API_BASE,
                "model_name": settings.LLM_MODEL_NAME,
            }
        )
        and is_loopback_endpoint(settings.EMBEDDING_API_BASE_URL)
        and not cloud_model_name(settings.EMBEDDING_MODEL)
        and is_loopback_endpoint(settings.QDRANT_URI)
    )
