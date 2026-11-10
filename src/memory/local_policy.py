"""Keep personal-memory helpers local regardless of chat-model approval."""

from __future__ import annotations

from src.config import settings
from src.midplatform.data_policy import is_loopback_endpoint, model_is_local


def cloud_model_name(name: str) -> bool:
    """Detect cloud-routed names carrying :cloud or -cloud markers."""
    value = name.lower()
    return ":cloud" in value or value.endswith("-cloud")


def local_memory_configuration() -> bool:
    """Require local, noncloud helper models, embedding endpoints, and vector storage."""
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
