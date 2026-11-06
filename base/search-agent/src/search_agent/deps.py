from dataclasses import dataclass

import httpx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from search_agent.config import settings


@dataclass
class PipelineDeps:
    """Shared dependencies injected into pipeline agents."""

    http_client: httpx.AsyncClient
    model: OpenAIChatModel


# Module-level shared state
_http_client: httpx.AsyncClient | None = None
_llm_client: httpx.AsyncClient | None = None
_model: OpenAIChatModel | None = None


def create_model(llm_client: httpx.AsyncClient | None = None) -> OpenAIChatModel:
    """Create an LLM model, optionally with a shared HTTP client."""
    return OpenAIChatModel(
        settings.llm_model,
        provider=OpenAIProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            http_client=llm_client
            or httpx.AsyncClient(
                timeout=httpx.Timeout(settings.llm_timeout, connect=10.0),
            ),
        ),
        profile=OpenAIModelProfile(
            openai_supports_strict_tool_definition=settings.llm_strict_tools,
        ),
    )


def init_shared_clients() -> None:
    """Create shared HTTP clients. Call during app lifespan startup."""
    global _http_client, _llm_client, _model

    _http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        timeout=httpx.Timeout(settings.searxng_timeout, connect=10.0),
    )
    _llm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.llm_timeout, connect=10.0),
    )
    _model = create_model(llm_client=_llm_client)


async def close_shared_clients() -> None:
    """Close shared HTTP clients. Call during app lifespan shutdown."""
    global _http_client, _llm_client, _model

    if _http_client:
        await _http_client.aclose()
        _http_client = None
    if _llm_client:
        await _llm_client.aclose()
        _llm_client = None
    _model = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared HTTP client for search-provider and page-fetch traffic."""
    if _http_client is None:
        raise RuntimeError("Call init_shared_clients() first")
    return _http_client


def get_model() -> OpenAIChatModel:
    """Return the shared LLM model."""
    if _model is None:
        raise RuntimeError("Call init_shared_clients() first")
    return _model
