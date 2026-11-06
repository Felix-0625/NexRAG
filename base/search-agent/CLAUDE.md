# Optional AarhusAI search reference

This service is an upstream reference retained with its own license. Native NexRAG does not launch this independent multi-Agent pipeline or a Redis service. The product single Agent calls the core authorized search tool with the configured search endpoint. See root `README.md`, `docs/local-run.md` and `src/agent/tools.py` for the product boundary.

## Independent reference development

From this directory with Python 3.12 and uv:

```shell
uv sync --frozen --python 3.12
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

There are no container build or orchestration files here. Do not use removed Taskfile recipes. Explicitly provision any endpoint used in standalone reference experiments. `SEARCH_AGENT_CACHE_BACKEND=disabled` avoids a separate cache service. Do not attach a reference experiment to employee data or call it product acceptance.

## Reference architecture

The inherited pipeline plans queries, executes the configured search provider, optionally fetches pages, and synthesizes a response. Its MCP surface exposes search without the full synthesis loop. These inherited capabilities do not establish that native NexRAG invokes them.

Preserve SSRF restrictions, query/result bounds, URL validation, timeout handling and source attribution. External search queries can reveal private data; internet retrieval is not inherently safe just because it reads public pages. Product account and data policies must authorize outbound queries before transmission. Core secret data remains local.

Keep `pyproject.toml` and `uv.lock` synchronized. Unit tests use isolated providers and must not silently call real network endpoints. Run source checks separately from real endpoint acceptance. Do not place API keys, private queries, results or environment-file contents in logs, fixtures or commits.
