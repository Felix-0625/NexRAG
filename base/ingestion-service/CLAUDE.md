# NexRAG ingestion maintenance

This vendored AarhusAI service retains its own `LICENSE.txt` and upstream attribution. NexRAG starts it as a native loopback process; this directory has no container build or orchestration entry point. The root `README.md`, `docs/local-run.md`, and `scripts/launch_local.py` describe the product runtime. Inherited upstream features are not automatically enabled NexRAG features.

## Development commands

From this directory with Python 3.12 and uv:

```shell
uv sync --frozen --extra dev --python 3.12
uv run pytest -q
uv run pytest tests/test_pipelines.py tests/test_bm42_idf.py -q
```

Do not start a second service against an occupied product port. Live service acceptance belongs to the root launch and browser workflow. Keep `pyproject.toml` and `uv.lock` consistent. The temporary Pillow 13 compatibility override is documented and must not be removed before checking the relevant image/BM42 compatibility and dependency advisories.

## Maintained path

- `app/main.py`: FastAPI lifespan, authentication and readiness.
- `app/routes/ingest.py`: authenticated multipart/JSON ingestion; NexRAG's WebUI bridge uses multipart local files.
- `app/pipelines/indexing.py`: native extraction, splitting, dense embedding, sparse document encoding and Qdrant write.
- `app/pipelines/splitter.py`: configured splitting and Child/Parent payload formation.
- `app/services/qdrant_setup.py`: payload indexes and non-destructive BM42 IDF upgrades.
- Root `scripts/local_ingestion.py`: native profile entry point.
- Root `webui/backend/open_webui/utils/nexrag_native_rag.py` is generated from the maintained WebUI patch; update the patch as well as generated source.

Only Child blocks receive dense and sparse vectors. Store canonical `parent_id` and `parent_text` in each Child payload; Parent text does not belong in another SQLite or Redis store. Identical content in different libraries must retain different scoped IDs. Overwrite one file revision and remove only its older points; never delete another library's copy. Sparse BM42 requires an IDF modifier on its Qdrant sparse index.

`/health` is liveness. `/health/ready` also waits for the pipeline and Qdrant. Neither proves ingestion quality or current user permissions. The loopback service credential is server-side; WebUI authorizes file ownership and destination library before calling it.

## Native parsing and optional upstream paths

The root native profile uses Python document readers for supported text-bearing files. It does not require Gotenberg, Kreuzberg, S3 or an office-conversion service. The source retains optional upstream adapters for reference; they require separate explicit configuration and are outside native acceptance unless exercised.

Keep heavy or optional imports lazy where possible. Do not accept a failed embedder as a successful write, report merely queued work as indexed, weaken file/library scope, or log document bodies or service credentials. Source and service tests are auxiliary evidence; real webpage upload, update, publish and access-denial checks are recorded separately in `docs/frontend-acceptance.md`.
