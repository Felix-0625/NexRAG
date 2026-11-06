import logging

import httpx
from pydantic import ValidationError

from search_agent import cache
from search_agent.config import settings
from search_agent.models import RawSearchResult
from search_agent.providers.base import is_valid_url, normalize_query, read_capped_json

logger = logging.getLogger(__name__)


class SearxngProvider:
    """Search backend backed by a self-hosted SearXNG instance."""

    name = "searxng"
    content_result_cap = None  # SearXNG never populates content from search

    async def search(self, client: httpx.AsyncClient, query: str) -> list[RawSearchResult]:
        """Execute a single search query against SearXNG and return structured results."""
        cache_key = cache.make_key(self.name, normalize_query(query), settings.searxng_url)
        cached = await cache.get_json(cache_key)
        if cached is not None:
            logger.debug("searxng cache hit query=%r", query)
            return [RawSearchResult.model_validate(item) for item in cached]

        try:
            async with client.stream(
                "GET",
                f"{settings.searxng_url}/search",
                params={"q": query, "format": "json"},
                timeout=settings.searxng_timeout,
            ) as response:
                response.raise_for_status()
                data = await read_capped_json(response, settings.searxng_max_response_bytes)
        except httpx.HTTPError:
            logger.exception("SearXNG request failed for query: %s", query)
            return []
        if data is None:
            return []

        unresponsive = data.get("unresponsive_engines") or []
        if unresponsive:
            logger.warning("SearXNG engines unresponsive for query=%r: %s", query, unresponsive)
        raw_results = data.get("results", [])
        logger.debug(
            "SearXNG response for query=%r: %d results, engines=%s",
            query,
            len(raw_results),
            sorted({item.get("engine", "unknown") for item in raw_results}),
        )

        results = []
        for item in raw_results:
            # ``or ""`` (not ``get(..., "")``) so an explicit JSON null becomes
            # an empty string instead of None, which the required str fields
            # would reject.
            title = item.get("title") or ""
            url = item.get("url") or ""
            snippet = item.get("content") or ""
            engine = item.get("engine") or "unknown"
            if not (title and url and is_valid_url(url)):
                continue
            try:
                results.append(
                    RawSearchResult(title=title, url=url, snippet=snippet, engine=engine)
                )
            except ValidationError:
                # A single malformed item must not sink the whole query's results.
                logger.warning("Skipping malformed SearXNG result for url=%r", url)

        if results:
            await cache.set_json(
                cache_key,
                [r.model_dump(mode="json") for r in results],
                ttl=settings.cache_searxng_ttl,
            )
        return results

    async def health(self, client: httpx.AsyncClient) -> bool:
        """Probe the SearXNG healthz endpoint."""
        try:
            response = await client.get(f"{settings.searxng_url}/healthz", timeout=5.0)
            return response.is_success
        except httpx.HTTPError:
            logger.exception("SearXNG health check failed")
            return False
