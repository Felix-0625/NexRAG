from unittest.mock import AsyncMock, patch

import httpx

from search_agent.fetch import fetch_pages
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
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_fetch = AsyncMock(return_value="fetched text")
        with patch("search_agent.fetch._fetch_one", mock_fetch):
            out = await fetch_pages(mock_client, results, 5, 10.0, 5000, 2_000_000)

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
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_fetch = AsyncMock(return_value="fetched text")
        with patch("search_agent.fetch._fetch_one", mock_fetch):
            await fetch_pages(mock_client, results, 2, 10.0, 5000, 2_000_000)

        fetched_urls = [c.args[1] for c in mock_fetch.call_args_list]
        assert fetched_urls == ["https://example.com/2", "https://example.com/3"]

    async def test_all_results_have_content_no_fetch(self):
        results = [make_result(0, content="a"), make_result(1, content="b")]
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_fetch = AsyncMock()
        with patch("search_agent.fetch._fetch_one", mock_fetch):
            out = await fetch_pages(mock_client, results, 5, 10.0, 5000, 2_000_000)

        mock_fetch.assert_not_called()
        assert out is results
