from unittest.mock import AsyncMock, MagicMock

import httpx

from search_agent.models import RawSearchResult
from search_agent.providers.base import is_valid_url, search_multiple
from search_agent.providers.searxng import SearxngProvider
from tests.conftest import make_stream_mock

provider = SearxngProvider()


class TestIsValidUrl:
    def test_http_url(self):
        assert is_valid_url("http://example.com") is True

    def test_https_url(self):
        assert is_valid_url("https://example.com/path?q=1") is True

    def test_javascript_url(self):
        assert is_valid_url("javascript:alert(1)") is False

    def test_file_url(self):
        assert is_valid_url("file:///etc/passwd") is False

    def test_data_url(self):
        assert is_valid_url("data:text/html,<h1>hi</h1>") is False

    def test_empty_string(self):
        assert is_valid_url("") is False

    def test_no_scheme(self):
        assert is_valid_url("example.com") is False

    def test_scheme_only(self):
        assert is_valid_url("https://") is False

    def test_ftp_url(self):
        assert is_valid_url("ftp://files.example.com") is False


class TestRawSearchResultTruncation:
    def test_truncates_long_title(self):
        result = RawSearchResult(
            title="A" * 300,
            url="https://example.com",
            snippet="short",
            engine="google",
        )
        assert len(result.title) == 200

    def test_truncates_long_snippet(self):
        result = RawSearchResult(
            title="Short",
            url="https://example.com",
            snippet="B" * 700,
            engine="google",
        )
        assert len(result.snippet) == 500

    def test_short_values_unchanged(self):
        result = RawSearchResult(
            title="Normal title",
            url="https://example.com",
            snippet="Normal snippet",
            engine="google",
        )
        assert result.title == "Normal title"
        assert result.snippet == "Normal snippet"


class TestSearXNGSearch:
    async def test_search_returns_structured_results(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(
            return_value=make_stream_mock(
                {
                    "results": [
                        {
                            "title": "Test Result",
                            "url": "https://example.com",
                            "content": "A test snippet",
                            "engine": "google",
                        },
                        {
                            "title": "Another Result",
                            "url": "https://example.org",
                            "content": "Another snippet",
                            "engine": "bing",
                        },
                    ]
                }
            )
        )

        results = await provider.search(mock_client, "test query")

        assert len(results) == 2
        assert results[0].title == "Test Result"
        assert results[0].url == "https://example.com"
        assert results[0].snippet == "A test snippet"
        assert results[0].engine == "google"

    async def test_search_handles_empty_results(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(return_value=make_stream_mock({"results": []}))

        results = await provider.search(mock_client, "empty query")
        assert results == []

    async def test_search_handles_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(side_effect=httpx.ConnectError("Connection failed"))

        results = await provider.search(mock_client, "failing query")
        assert results == []

    async def test_search_filters_invalid_urls(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(
            return_value=make_stream_mock(
                {
                    "results": [
                        {
                            "title": "Good",
                            "url": "https://example.com",
                            "content": "ok",
                            "engine": "google",
                        },
                        {
                            "title": "JS injection",
                            "url": "javascript:alert(1)",
                            "content": "bad",
                            "engine": "google",
                        },
                        {
                            "title": "File access",
                            "url": "file:///etc/passwd",
                            "content": "bad",
                            "engine": "google",
                        },
                        {
                            "title": "Also good",
                            "url": "http://example.org",
                            "content": "ok",
                            "engine": "bing",
                        },
                    ]
                }
            )
        )

        results = await provider.search(mock_client, "mixed urls")

        assert len(results) == 2
        assert results[0].url == "https://example.com"
        assert results[1].url == "http://example.org"

    async def test_search_multiple_deduplicates(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(
            side_effect=[
                make_stream_mock(
                    {
                        "results": [
                            {
                                "title": "A",
                                "url": "https://a.com",
                                "content": "A",
                                "engine": "google",
                            },
                            {
                                "title": "B",
                                "url": "https://b.com",
                                "content": "B",
                                "engine": "google",
                            },
                        ]
                    }
                ),
                make_stream_mock(
                    {
                        "results": [
                            {
                                "title": "B dup",
                                "url": "https://b.com",
                                "content": "B again",
                                "engine": "bing",
                            },
                            {
                                "title": "C",
                                "url": "https://c.com",
                                "content": "C",
                                "engine": "bing",
                            },
                        ]
                    }
                ),
            ]
        )

        results = await search_multiple(provider, mock_client, ["query1", "query2"])

        assert len(results) == 3
        urls = [r.url for r in results]
        assert "https://a.com" in urls
        assert "https://b.com" in urls
        assert "https://c.com" in urls

    async def test_search_multiple_survives_one_failing_query(self):
        # If one query raises unexpectedly, the others' results are still
        # returned (gather uses return_exceptions=True) rather than the whole
        # search failing.
        class FlakyProvider:
            name = "flaky"
            content_result_cap = None

            async def search(self, client, query):
                if query == "boom":
                    raise ValueError("provider blew up")
                return [
                    RawSearchResult(title="ok", url="https://ok.com", snippet="ok", engine="flaky")
                ]

            async def health(self, client):
                return True

        results = await search_multiple(FlakyProvider(), AsyncMock(), ["boom", "fine"])

        assert len(results) == 1
        assert results[0].url == "https://ok.com"
