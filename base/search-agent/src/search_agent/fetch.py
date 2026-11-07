import asyncio
import ipaddress
import logging
import socket

import httpx
import trafilatura

from search_agent import cache
from search_agent.config import settings
from search_agent.models import RawSearchResult
from search_agent.providers.base import is_valid_url

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


async def _public_ip(host: str, timeout: float) -> str | None:
    """Resolve once and select an address only if all answers are public unicast."""
    try:
        async with asyncio.timeout(timeout):
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    except (socket.gaierror, TimeoutError, ValueError):
        return None
    if not addresses or any(
        not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses
    ):
        return None
    return str(min(addresses, key=lambda ip: ip.version))


async def _fetch_one(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    max_chars: int,
    max_bytes: int,
) -> str | None:
    """Fetch a URL and return extracted main text, or None on any failure.

    Results are cached: successful extracts for `cache_fetch_ttl`, failures
    (invalid URL, SSRF block, HTTP error, empty extract) for the shorter
    `cache_fetch_negative_ttl` so transient failures recover quickly.
    """
    cache_key = cache.make_key("fetch-public-ip", url, max_chars)
    cached = await cache.get_json(cache_key)
    if cached is not None:
        logger.debug("fetch cache hit url=%s ok=%s", url, cached.get("ok"))
        return cached.get("text") if cached.get("ok") else None

    text = await _fetch_one_uncached(client, url, timeout, max_chars, max_bytes)
    if text:
        await cache.set_json(cache_key, {"ok": True, "text": text}, ttl=settings.cache_fetch_ttl)
    else:
        await cache.set_json(cache_key, {"ok": False}, ttl=settings.cache_fetch_negative_ttl)
    return text


async def _fetch_one_uncached(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    max_chars: int,
    max_bytes: int,
) -> str | None:
    body = await _fetch_with_validated_redirects(client, url, timeout, max_bytes)
    if not body:
        return None

    text = await asyncio.to_thread(
        trafilatura.extract,
        body,
        include_comments=False,
        include_tables=False,
        output_format="markdown",
    )
    if not text:
        return None
    return text[:max_chars]


async def _fetch_with_validated_redirects(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    max_bytes: int,
) -> str | None:
    """Validate each redirect and connect to the exact checked address."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not is_valid_url(current):
            return None
        try:
            target = httpx.URL(current)
            if not target.host or target.userinfo:
                return None
            host = target.raw_host.decode("ascii")
            address = await _public_ip(host, timeout)
            if address is None:
                logger.debug("Fetch blocked (non-public host): %s", host)
                return None
            # Keep the original HTTP authority and TLS certificate identity when dialing the IP.
            request = httpx.Request(
                "GET",
                target.copy_with(host=address),
                headers={"Host": target.netloc.decode("ascii"), "Accept-Encoding": "identity"},
                extensions={"sni_hostname": host, "timeout": httpx.Timeout(timeout).as_dict()},
            )
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = str(target.join(location))
                    continue
                if response.status_code >= 400:
                    return None
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("text/html"):
                    return None
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return None

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        logger.debug("Fetch aborted (size cap %d exceeded): %s", max_bytes, current)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            finally:
                await response.aclose()
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeDecodeError):
            logger.debug("Fetch failed: %s", current, exc_info=True)
            return None
    logger.debug("Fetch aborted (max redirects exceeded): %s", url)
    return None


async def fetch_pages(
    results: list[RawSearchResult],
    max_pages: int,
    timeout: float,
    max_chars: int,
    max_bytes: int,
) -> list[RawSearchResult]:
    """Fetch and extract main content for up to max_pages results.

    Results that already carry content (a provider like Staan can return
    full page text with the search response) are left untouched; only the
    first max_pages results without content are fetched. Any fetch failure
    leaves content=None so the pipeline continues with just the snippet.
    """
    if max_pages <= 0 or not results:
        return results

    targets = [r for r in results if not r.content][:max_pages]
    if not targets:
        return results
    # Public fetches never use the provider's session or an environment proxy.
    # Connections are not pooled across hostnames that resolve to the same IP.
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=0)
    async with httpx.AsyncClient(trust_env=False, limits=limits) as client:
        extracted = await asyncio.gather(
            *(_fetch_one(client, r.url, timeout, max_chars, max_bytes) for r in targets)
        )
    for result, text in zip(targets, extracted, strict=True):
        result.content = text

    fetched = sum(1 for t in extracted if t)
    logger.info("Fetched %d/%d pages with extractable content", fetched, len(targets))
    return results
