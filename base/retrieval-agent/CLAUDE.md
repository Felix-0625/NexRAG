# NexRAG retrieval maintenance

This vendored AarhusAI service retains its own `LICENSE.txt` and attribution. NexRAG uses its native retrieval pipeline as a tool beneath the root single Agent. Do not reintroduce a second retrieval Agent into the product loop. Root `README.md`, `docs/local-run.md` and `scripts/launch_local.py` define the current runtime; there is no container orchestration entry point in this directory.

## Development commands

From this directory with Python 3.12 and uv:

```shell
uv sync --frozen --extra dev --python 3.12
uv run pytest -q
uv run pytest tests/services/test_pipeline.py tests/services/test_reranker.py -q
```

Keep the lock file synchronized. Do not start an extra service on product ports or change running indexes as part of unit tests. Test fixtures should use isolated collections and delete only their own synthetic resources.

## Maintained query path

- `app/routes/search.py`: authenticated `/search` endpoint accepting queries and library scopes.
- `app/services/pipeline.py`: linear retrieval path, reranking and Parent expansion.
- `app/services/qdrant.py`: Qdrant scope filters and native multi-vector RRF.
- `app/services/sparse_embedding.py`: BM42 query encoding, using FastEmbed `query_embed` rather than document `embed`.
- `app/services/reranker.py`: cross-encoder scores and thresholds.
- Root `scripts/local_retrieval.py`: native service entry point.

The server-verified library list is authority; user text cannot enlarge it. Apply the same scope filter to dense and sparse branches. Fuse coarse Child candidates with Qdrant RRF, score Child text with the cross-encoder, retain Top-3 above threshold, then expand complete Parent text from payload and deduplicate by scoped source. Deduplication may produce fewer than three parents; do not backfill low-ranked candidates after selection.

New indexing uses `parent_text`; legacy `parent_content` remains readable. Query metadata must not duplicate the entire Parent text into responses. Required reranking fails closed rather than reporting coarse-only results as cross-encoder verified. A numeric relevance score is not a calibrated probability of truth.

`/health` proves liveness; `/health/ready` checks dependencies. The public `scripts/verify_rag_native.py` exercises actual local embedding, sparse encoding, reranking and Qdrant with disposable synthetic collections. It does not prove account login, webpage uploads or a target-corpus retrieval benchmark. Those require distinct evidence.

The source retains upstream agentic/query-generation modes for reference. Native NexRAG supplies explicit queries from the root Agent and uses the linear tool path. Do not infer an enabled product feature from inherited source alone. Never log private query/document contents or service keys, weaken ACL filters, or delete shared collections during tests.
