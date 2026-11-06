from unittest.mock import ANY, AsyncMock, MagicMock, patch

from search_agent.models import (
    RawSearchResult,
    SearchResult,
    Source,
)
from search_agent.pipeline import _is_simple_query


class TestSearchPipeline:
    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_pipeline_runs_all_stages(
        self, mock_get_model, mock_get_http_client, mock_planner, mock_search, mock_analyze_synth
    ):
        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()

        # Mock planner output
        planner_result = MagicMock()
        planner_result.output = ["search query 1", "search query 2"]
        mock_planner.run = AsyncMock(return_value=planner_result)

        # Mock search results
        mock_search.return_value = [
            RawSearchResult(
                title="Result 1",
                url="https://example.com",
                snippet="A snippet",
                engine="google",
            ),
        ]

        # Mock analyze+synthesize output
        analyze_synth_result = MagicMock()
        analyze_synth_result.output = SearchResult(
            summary="Here is the answer [1].",
            sources=[Source(title="Example", url="https://example.com")],
        )
        mock_analyze_synth.run = AsyncMock(return_value=analyze_synth_result)

        result = await run_search_pipeline(
            "compare python vs rust for web development", "some context"
        )

        assert result.summary == "Here is the answer [1]."
        assert len(result.sources) == 1
        assert result.sources[0].url == "https://example.com"

        mock_planner.run.assert_called_once()
        mock_search.assert_called_once()
        mock_analyze_synth.run.assert_called_once()

    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_pipeline_empty_results(
        self, mock_get_model, mock_get_http_client, mock_planner, mock_search
    ):
        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()

        planner_result = MagicMock()
        planner_result.output = ["search query"]
        mock_planner.run = AsyncMock(return_value=planner_result)

        mock_search.return_value = []

        result = await run_search_pipeline("compare apples vs oranges")

        assert result.summary == "No search results found."
        assert result.sources == []

    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.settings")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_pipeline_skips_planner_for_simple_query(
        self, mock_get_model, mock_get_http_client, mock_settings, mock_search, mock_analyze_synth
    ):
        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()
        mock_settings.search_skip_planner_for_simple_queries = True
        mock_settings.search_pipeline_timeout = 90
        mock_settings.datetime_timezone = "UTC"
        mock_settings.datetime_format = "%Y-%m-%d"
        mock_settings.search_simple_query_max_words = 15
        mock_settings.search_simple_query_max_questions = 1
        mock_settings.search_max_queries = 3
        mock_settings.search_max_results = 15
        mock_settings.search_fetch_page_content = False

        mock_search.return_value = [
            RawSearchResult(
                title="Result 1",
                url="https://example.com",
                snippet="Population info",
                engine="google",
            ),
        ]

        analyze_synth_result = MagicMock()
        analyze_synth_result.output = SearchResult(
            summary="The answer [1].",
            sources=[Source(title="Example", url="https://example.com")],
        )
        mock_analyze_synth.run = AsyncMock(return_value=analyze_synth_result)

        result = await run_search_pipeline("What is the population of Denmark?")

        assert result.summary == "The answer [1]."
        # Planner should not have been called — search_multiple gets the raw query
        mock_search.assert_called_once_with(
            ANY, mock_get_http_client.return_value, ["What is the population of Denmark?"]
        )


class TestIsSimpleQuery:
    def test_short_simple_query(self):
        assert _is_simple_query("What is the weather today?") is True

    def test_long_query_not_simple(self):
        query = (
            "Tell me about the history of artificial intelligence and how it"
            " relates to modern computing and what the future holds for"
            " machine learning applications in healthcare"
        )
        assert _is_simple_query(query) is False

    def test_compare_query_not_simple(self):
        assert _is_simple_query("compare Python vs Rust") is False

    def test_difference_query_not_simple(self):
        assert _is_simple_query("difference between TCP and UDP") is False

    def test_pros_and_cons_not_simple(self):
        assert _is_simple_query("pros and cons of microservices") is False

    def test_multiple_questions_not_simple(self):
        assert _is_simple_query("What is Python? What is Rust?") is False

    def test_single_word_query_simple(self):
        assert _is_simple_query("Denmark") is True

    def test_versus_not_simple(self):
        assert _is_simple_query("React versus Vue for frontend") is False


class TestSearchPipelineRaw:
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_raw_pipeline_returns_raw_results(
        self, mock_get_model, mock_get_http_client, mock_planner, mock_search
    ):
        from search_agent.pipeline import run_search_pipeline_raw

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()

        planner_result = MagicMock()
        planner_result.output = ["search query"]
        mock_planner.run = AsyncMock(return_value=planner_result)

        expected = [
            RawSearchResult(
                title="Result 1", url="https://a.com", snippet="Snippet 1", engine="google"
            ),
            RawSearchResult(
                title="Result 2", url="https://b.com", snippet="Snippet 2", engine="bing"
            ),
        ]
        mock_search.return_value = expected

        result = await run_search_pipeline_raw("test query")

        assert result == expected

    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_raw_pipeline_empty_results(
        self, mock_get_model, mock_get_http_client, mock_planner, mock_search
    ):
        from search_agent.pipeline import run_search_pipeline_raw

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()

        planner_result = MagicMock()
        planner_result.output = ["query"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        mock_search.return_value = []

        result = await run_search_pipeline_raw("empty query")

        assert result == []

    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_raw_pipeline_caps_at_15(
        self, mock_get_model, mock_get_http_client, mock_planner, mock_search
    ):
        from search_agent.pipeline import run_search_pipeline_raw

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()

        planner_result = MagicMock()
        planner_result.output = ["query"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        mock_search.return_value = [
            RawSearchResult(title=f"R{i}", url=f"https://{i}.com", snippet=f"S{i}", engine="google")
            for i in range(20)
        ]

        result = await run_search_pipeline_raw("big query")

        assert len(result) == 15


class TestSearchCountSettings:
    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.settings")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_planner_output_capped_by_max_queries(
        self,
        mock_get_model,
        mock_get_http_client,
        mock_settings,
        mock_planner,
        mock_search,
        mock_analyze_synth,
    ):
        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()
        mock_settings.search_skip_planner_for_simple_queries = False
        mock_settings.search_pipeline_timeout = 90
        mock_settings.datetime_timezone = "UTC"
        mock_settings.datetime_format = "%Y-%m-%d"
        mock_settings.search_max_queries = 2
        mock_settings.search_max_results = 15
        mock_settings.search_fetch_page_content = False

        planner_result = MagicMock()
        planner_result.output = ["q1", "q2", "q3", "q4"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        mock_search.return_value = [
            RawSearchResult(title="T", url="https://e.com", snippet="s", engine="g"),
        ]

        analyze_synth_result = MagicMock()
        analyze_synth_result.output = SearchResult(summary="ok", sources=[])
        mock_analyze_synth.run = AsyncMock(return_value=analyze_synth_result)

        await run_search_pipeline("a long multi-part query needing real planning")

        # search_multiple is called with at most max_queries queries
        called_queries = mock_search.call_args.args[2]
        assert called_queries == ["q1", "q2"]

    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.settings")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_results_capped_by_max_results(
        self,
        mock_get_model,
        mock_get_http_client,
        mock_settings,
        mock_planner,
        mock_search,
        mock_analyze_synth,
    ):
        import json

        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()
        mock_settings.search_skip_planner_for_simple_queries = True
        mock_settings.search_simple_query_max_words = 15
        mock_settings.search_simple_query_max_questions = 1
        mock_settings.search_pipeline_timeout = 90
        mock_settings.datetime_timezone = "UTC"
        mock_settings.datetime_format = "%Y-%m-%d"
        mock_settings.search_max_queries = 3
        mock_settings.search_max_results = 3
        mock_settings.search_fetch_page_content = False

        planner_result = MagicMock()
        planner_result.output = ["q"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        mock_search.return_value = [
            RawSearchResult(title=f"T{i}", url=f"https://{i}.com", snippet=f"s{i}", engine="g")
            for i in range(10)
        ]

        analyze_synth_result = MagicMock()
        analyze_synth_result.output = SearchResult(summary="ok", sources=[])
        mock_analyze_synth.run = AsyncMock(return_value=analyze_synth_result)

        await run_search_pipeline("simple query")

        # The raw_json passed to synthesizer contains only max_results items
        prompt = mock_analyze_synth.run.call_args.args[0]
        json_blob = prompt.split("--- BEGIN EXTERNAL SEARCH RESULTS ---\n")[1].split("\n--- END")[0]
        parsed = json.loads(json_blob)
        assert len(parsed) == 3


class TestSimpleQueryThresholds:
    @patch("search_agent.pipeline.settings")
    def test_word_threshold_respects_setting(self, mock_settings):
        mock_settings.search_simple_query_max_words = 3
        mock_settings.search_simple_query_max_questions = 1
        # 4 words > 3 → not simple
        assert _is_simple_query("one two three four") is False
        # 3 words ≤ 3 → simple
        assert _is_simple_query("one two three") is True

    @patch("search_agent.pipeline.settings")
    def test_question_threshold_respects_setting(self, mock_settings):
        mock_settings.search_simple_query_max_words = 50
        mock_settings.search_simple_query_max_questions = 2
        # 2 ?s allowed
        assert _is_simple_query("What? Why?") is True
        # 3 ?s blocked
        assert _is_simple_query("What? Why? How?") is False


class TestFetchIntegration:
    @patch("search_agent.pipeline.fetch_pages")
    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.settings")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_fetch_disabled_skips_fetch(
        self,
        mock_get_model,
        mock_get_http_client,
        mock_settings,
        mock_planner,
        mock_search,
        mock_analyze_synth,
        mock_fetch_pages,
    ):
        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()
        mock_settings.search_skip_planner_for_simple_queries = True
        mock_settings.search_simple_query_max_words = 15
        mock_settings.search_simple_query_max_questions = 1
        mock_settings.search_pipeline_timeout = 90
        mock_settings.datetime_timezone = "UTC"
        mock_settings.datetime_format = "%Y-%m-%d"
        mock_settings.search_max_queries = 3
        mock_settings.search_max_results = 15
        mock_settings.search_fetch_page_content = False

        planner_result = MagicMock()
        planner_result.output = ["q"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        mock_search.return_value = [
            RawSearchResult(title="T", url="https://e.com", snippet="s", engine="g"),
        ]
        analyze_synth_result = MagicMock()
        analyze_synth_result.output = SearchResult(summary="ok", sources=[])
        mock_analyze_synth.run = AsyncMock(return_value=analyze_synth_result)

        await run_search_pipeline("simple query")

        mock_fetch_pages.assert_not_called()

    @patch("search_agent.pipeline.fetch_pages")
    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.settings")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_fetch_enabled_calls_fetch_and_content_reaches_synth(
        self,
        mock_get_model,
        mock_get_http_client,
        mock_settings,
        mock_planner,
        mock_search,
        mock_analyze_synth,
        mock_fetch_pages,
    ):
        import json

        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()
        mock_settings.search_skip_planner_for_simple_queries = True
        mock_settings.search_simple_query_max_words = 15
        mock_settings.search_simple_query_max_questions = 1
        mock_settings.search_pipeline_timeout = 90
        mock_settings.datetime_timezone = "UTC"
        mock_settings.datetime_format = "%Y-%m-%d"
        mock_settings.search_max_queries = 3
        mock_settings.search_max_results = 15
        mock_settings.search_fetch_page_content = True
        mock_settings.search_fetch_max_pages = 5
        mock_settings.search_fetch_timeout = 10
        mock_settings.search_fetch_max_chars = 5000
        mock_settings.search_fetch_max_bytes = 2_000_000

        planner_result = MagicMock()
        planner_result.output = ["q"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        raw = [
            RawSearchResult(title="T", url="https://e.com", snippet="s", engine="g"),
        ]
        mock_search.return_value = raw

        async def _fake_fetch(client, results, **kwargs):
            results[0].content = "Extracted main body text from the page."
            return results

        mock_fetch_pages.side_effect = _fake_fetch

        analyze_synth_result = MagicMock()
        analyze_synth_result.output = SearchResult(summary="ok", sources=[])
        mock_analyze_synth.run = AsyncMock(return_value=analyze_synth_result)

        await run_search_pipeline("simple query")

        mock_fetch_pages.assert_called_once()
        prompt = mock_analyze_synth.run.call_args.args[0]
        json_blob = prompt.split("--- BEGIN EXTERNAL SEARCH RESULTS ---\n")[1].split("\n--- END")[0]
        parsed = json.loads(json_blob)
        assert parsed[0]["content"] == "Extracted main body text from the page."

    @patch("search_agent.pipeline.fetch_pages")
    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.settings")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_fetch_failure_leaves_content_absent(
        self,
        mock_get_model,
        mock_get_http_client,
        mock_settings,
        mock_planner,
        mock_search,
        mock_analyze_synth,
        mock_fetch_pages,
    ):
        import json

        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http_client.return_value = MagicMock()
        mock_settings.search_skip_planner_for_simple_queries = True
        mock_settings.search_simple_query_max_words = 15
        mock_settings.search_simple_query_max_questions = 1
        mock_settings.search_pipeline_timeout = 90
        mock_settings.datetime_timezone = "UTC"
        mock_settings.datetime_format = "%Y-%m-%d"
        mock_settings.search_max_queries = 3
        mock_settings.search_max_results = 15
        mock_settings.search_fetch_page_content = True
        mock_settings.search_fetch_max_pages = 5
        mock_settings.search_fetch_timeout = 10
        mock_settings.search_fetch_max_chars = 5000
        mock_settings.search_fetch_max_bytes = 2_000_000

        planner_result = MagicMock()
        planner_result.output = ["q"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        raw = [
            RawSearchResult(title="T", url="https://e.com", snippet="s", engine="g"),
        ]
        mock_search.return_value = raw

        async def _fake_fetch(client, results, **kwargs):
            # Simulate every page failing — content stays None
            return results

        mock_fetch_pages.side_effect = _fake_fetch

        analyze_synth_result = MagicMock()
        analyze_synth_result.output = SearchResult(summary="ok", sources=[])
        mock_analyze_synth.run = AsyncMock(return_value=analyze_synth_result)

        await run_search_pipeline("simple query")

        prompt = mock_analyze_synth.run.call_args.args[0]
        json_blob = prompt.split("--- BEGIN EXTERNAL SEARCH RESULTS ---\n")[1].split("\n--- END")[0]
        parsed = json.loads(json_blob)
        # exclude_none=True → content key absent when None
        assert "content" not in parsed[0]
