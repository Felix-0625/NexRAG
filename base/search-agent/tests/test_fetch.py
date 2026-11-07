import asyncio
import socket
from unittest.mock import AsyncMock, patch

import httpx

from search_agent.fetch import _fetch_with_validated_redirects, fetch_pages
from search_agent.models import RawSearchResult


def make_result(i: int, content: str | None = None) -> RawSearchResult:
    return RawSearchResult(
        title=f"T{i}",
        url=f"https://example.com/{i}",
        snippet=f"s{i}",
        engine="test",
        content=content,
    )


class TestFetchPagesSkipsExistingContent:
    async def test_skips_results_with_content(self):
        results = [
            make_result(0, content="already have full text"),
            make_result(1),
            make_result(2),
        ]
        mock_fetch = AsyncMock(return_value="fetched text")
        with patch("search_agent.fetch._fetch_one", mock_fetch):
            out = await fetch_pages(results, 5, 10.0, 5000, 2_000_000)

        fetched_urls = [c.args[1] for c in mock_fetch.call_args_list]
        assert fetched_urls == ["https://example.com/1", "https://example.com/2"]
        assert out[0].content == "already have full text"  # untouched
        assert out[1].content == "fetched text"
        assert out[2].content == "fetched text"

    async def test_max_pages_counts_only_contentless_results(self):
        # Content-bearing results must not consume max_pages slots.
        results = [
            make_result(0, content="pre"),
            make_result(1, content="pre"),
            make_result(2),
            make_result(3),
            make_result(4),
        ]
        mock_fetch = AsyncMock(return_value="fetched text")
        with patch("search_agent.fetch._fetch_one", mock_fetch):
            await fetch_pages(results, 2, 10.0, 5000, 2_000_000)

        fetched_urls = [c.args[1] for c in mock_fetch.call_args_list]
        assert fetched_urls == ["https://example.com/2", "https://example.com/3"]

    async def test_all_results_have_content_no_fetch(self):
        results = [make_result(0, content="a"), make_result(1, content="b")]
        mock_fetch = AsyncMock()
        with patch("search_agent.fetch._fetch_one", mock_fetch):
            out = await fetch_pages(results, 5, 10.0, 5000, 2_000_000)

        mock_fetch.assert_not_called()
        assert out is results


async def test_fetch_dials_checked_ip_and_blocks_private_redirect():
    requests = []

    async def resolve(host, *_args, **_kwargs):
        address = "93.184.215.14" if host == "example.com" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://internal.example/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", side_effect=resolve):
            assert (
                await _fetch_with_validated_redirects(client, "https://example.com:8443/", 1, 100)
                is None
            )

    assert len(requests) == 1
    assert requests[0].url.host == "93.184.215.14"
    assert requests[0].url.port == 8443
    assert requests[0].headers["Host"] == "example.com:8443"
    assert requests[0].extensions["sni_hostname"] == "example.com"
