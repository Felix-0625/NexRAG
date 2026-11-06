import json
from unittest.mock import AsyncMock, patch

from search_agent.models import RawSearchResult


class TestSearchWebMCPTool:
    @patch("search_agent.mcp_server.run_search_pipeline_raw", new_callable=AsyncMock)
    async def test_search_web_returns_openwebui_format(self, mock_raw):
        from search_agent.mcp_server import search_web

        mock_raw.return_value = [
            RawSearchResult(
                title="Title 1", url="https://a.com", snippet="Snippet 1", engine="google"
            ),
            RawSearchResult(
                title="Title 2", url="https://b.com", snippet="Snippet 2", engine="bing"
            ),
        ]

        result = await search_web("test query")
        parsed = json.loads(result)

        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert parsed[0] == {"title": "Title 1", "link": "https://a.com", "snippet": "Snippet 1"}
        assert parsed[1] == {"title": "Title 2", "link": "https://b.com", "snippet": "Snippet 2"}
        # Open WebUI expects 'link' not 'url', and no 'engine'
        assert "url" not in parsed[0]
        assert "engine" not in parsed[0]

    @patch("search_agent.mcp_server.run_search_pipeline_raw", new_callable=AsyncMock)
    async def test_search_web_empty_results(self, mock_raw):
        from search_agent.mcp_server import search_web

        mock_raw.return_value = []

        result = await search_web("nothing")
        parsed = json.loads(result)

        assert parsed == []
