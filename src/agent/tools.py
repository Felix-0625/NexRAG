"""Governed business, knowledge, web-search, and sandbox-file tools.

Business reads use tenant snapshots. AarhusAI retrieval owns Qdrant indexing, fusion, and
reranking; NexRAG does not duplicate it. Web search delegates to SearXNG or public APIs with
fallbacks. Temporary file analysis stays in memory and never pollutes persistent knowledge.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import sqlglot
from sqlglot import exp

from src.config import DEFAULT_KNOWLEDGE_TOP_K, MAX_KNOWLEDGE_TOP_K, settings
from src.midplatform.adapters import load_source_profile
from src.midplatform.catalog import MetadataCatalog
from src.midplatform.sandbox_file import FileSandboxSecurityError, inspect_file_content


def register_plan(sub_tasks: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Validate and clean multi-step analysis subtasks."""
    clean_steps = [str(s).strip() for s in sub_tasks if str(s).strip()]
    if not clean_steps:
        return [], {"error": "Sub-tasks list cannot be empty."}
    return clean_steps, {
        "status": "plan_registered",
        "step_count": len(clean_steps),
        "sub_tasks": clean_steps,
        "instruction": "Proceed with tool calls to collect evidence for each planned sub-task.",
    }


def business_schema(
    metadata_path: str | Path,
    *,
    query: str | None = None,
    table_name: str | None = None,
    max_tables: int = 2,
) -> str:
    """Describe configured business views and documented metric formulas with dynamic pruning."""
    from src.midplatform.workspace import active_connection

    active = active_connection()
    catalog = MetadataCatalog(active["metadata_path"] if active else str(metadata_path))
    profile = load_source_profile(
        active["source_path"] if active else settings.BUSINESS_SOURCE_PATH,
        None if active else settings.BUSINESS_DB_PATH,
        active["dsn"] if active else settings.BUSINESS_DB_DSN,
    )
    tables = catalog.metadata.get("tables", {})
    if set(tables) != set(profile.views) or any(
        set(tables[name].get("columns", {})) != set(view.columns)
        for name, view in profile.views.items()
    ):
        raise ValueError("Business metadata does not match configured source views")

    if table_name:
        selected_tables = [table_name] if table_name in tables else []
    elif query:
        selected_tables = catalog.prune_schema(query, max_tables=max_tables)
    else:
        selected_tables = None

    summary = catalog.get_table_schema_summary(allowed_tables=selected_tables)
    metrics = catalog.metadata.get("metrics", {})
    metric_lines = [
        f"- {name}: {item['calculation']} ({item['description']}); kind={item.get('kind', 'number')}, unit={item.get('unit') or 'undeclared'}"
        for name, item in metrics.items()
        if not selected_tables or item.get("table") in selected_tables
    ]
    guidance = catalog.metadata.get("query_guidance", "")
    metrics_block = (
        "\n### Metrics\n" + "\n".join(metric_lines)
        if metric_lines
        else "\n### Metrics\nNo registered metrics in this schema selection. Never invent metric IDs.\n"
    )
    scope_note = (
        "These views already contain only the authorized tenant rows. "
        "Query them directly; do not add tenant/company/organization predicates. "
        "Do not invent status values or add filters the user did not ask for.\n"
    )
    grain_lines = [
        f"- {name}: grain={view.grain or 'undeclared'}, enforced_primary_key={list(view.primary_key)}"
        for name, view in profile.views.items()
        if not selected_tables or name in selected_tables
    ]
    return (
        scope_note
        + summary
        + metrics_block
        + "\n### Enforced row grain\n"
        + "\n".join(grain_lines)
        + "\n"
        + guidance
    )


def query_business(sql: str, tenant_id: str, db_path: str | Path | None) -> dict[str, Any]:
    """Execute bounded SQL in a private tenant snapshot."""
    if not tenant_id:
        raise ValueError("当前账号未被授予业务数据库访问权限")
    from src.agent.business_session import BusinessReadSession, current_business_session

    current = current_business_session()
    session = current or BusinessReadSession()
    try:
        return _query_business_in_session(session, sql, tenant_id, db_path)
    finally:
        if current is None:
            session.close()


def _query_business_in_session(session, sql, tenant_id, db_path):
    result = session.read(sql, tenant_id, db_path)
    rows = result["rows"]
    # Diagnose strict filters on the same authorized snapshot without altering or relaxing the original query.
    empty_aggregate = bool(rows) and all(
        value is None or value == 0 for row in rows for value in row.values()
    )
    tree = sqlglot.parse_one(sql, read="sqlite")
    if (not rows or empty_aggregate) and tree.find(exp.Where):
        samples = {}
        for table in tree.find_all(exp.Table):
            if table.name in samples or len(samples) >= 2:
                continue
            try:
                samples[table.name] = session.read(
                    f'SELECT * FROM "{table.name.replace(chr(34), chr(34) * 2)}" LIMIT 3',
                    tenant_id,
                    max_rows=3,
                )["rows"]
            except (ValueError, FileNotFoundError):
                continue
        if any(samples.values()):
            result["authorized_view_samples"] = samples
            result["reflection_hint"] = (
                "The authorized view is NOT empty. Your WHERE conditions excluded records. "
                "Compare these authorized samples and remove invented status/tenant filters; "
                "do not report that the source has no orders."
            )
    return result


async def search_knowledge(
    query: str,
    *,
    base_url: str,
    api_key: str,
    collection_name: str | list[str],
    client: httpx.AsyncClient | None = None,
    top_k: int = DEFAULT_KNOWLEDGE_TOP_K,
) -> list[dict[str, Any]]:
    """Call retrieval and normalize its parallel result lists."""
    if not query.strip():
        raise ValueError("Knowledge search query cannot be empty")
    if not api_key:
        raise RuntimeError("Knowledge retrieval API key is not configured")
    if not 1 <= top_k <= MAX_KNOWLEDGE_TOP_K:
        raise ValueError(f"Knowledge result count must be between 1 and {MAX_KNOWLEDGE_TOP_K}")

    collections = [collection_name] if isinstance(collection_name, str) else list(collection_name)
    if not collections:
        return []
    # The retrieval service permits at most 20 collections per call.
    if len(collections) > 20:
        results = []
        for start in range(0, len(collections), 20):
            results.extend(
                await search_knowledge(
                    query,
                    base_url=base_url,
                    api_key=api_key,
                    collection_name=collections[start : start + 20],
                    client=client,
                    top_k=top_k,
                )
            )
        return sorted(results, key=lambda item: item["score"], reverse=True)[:top_k]
    body = {"queries": [query[:2_000]], "collection_names": collections, "k": top_k}
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=settings.RETRIEVAL_TIMEOUT_SECONDS)
    try:
        response = await active_client.post(
            f"{base_url.rstrip('/')}/search",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            await active_client.aclose()

    documents = (payload.get("documents") or [[]])[0]
    metadatas = (payload.get("metadatas") or [[]])[0]
    distances = (payload.get("distances") or [[]])[0]
    if not (len(documents) == len(metadatas) == len(distances)):
        raise ValueError("Retrieval service returned mismatched result lists")
    return [
        {
            "source": metadata.get("source") or metadata.get("file_id") or "Unknown document",
            "content": content,
            "score": score,
            "metadata": metadata,
        }
        for content, metadata, score in zip(documents, metadatas, distances, strict=True)
    ]


async def search_web(
    query: str,
    *,
    base_url: str | None = None,
    searxng_url: str | None = None,
    timeout: float = 8.0,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Search SearXNG JSON with graceful fallback on failure."""
    if not query.strip():
        raise ValueError("Web search query cannot be empty")

    effective_url = (searxng_url or base_url or settings.SEARXNG_URL).rstrip("/")
    url = f"{effective_url}/search"
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=timeout)

    try:
        response = await active_client.get(
            url,
            params={"q": query[:500], "format": "json"},
            headers={"User-Agent": "NexRAG-Agent/0.1.0"},
        )
        response.raise_for_status()
        data = response.json()
        raw_results = data.get("results") or []
        formatted: list[dict[str, Any]] = []
        for item in raw_results[:5]:
            formatted.append(
                {
                    "title": item.get("title") or "Untitled",
                    "url": item.get("url") or "",
                    "snippet": (item.get("content") or "")[:800],
                    "engine": item.get("engine") or "web",
                }
            )
        return formatted
    except (httpx.HTTPError, ValueError) as exc:
        return [
            {
                "title": "Web Search Unavailable",
                "url": "",
                "snippet": f"External search request to {effective_url} failed or was offline: {exc}",
                "engine": "fallback",
            }
        ]
    finally:
        if owns_client:
            await active_client.aclose()


def inspect_temporary_file(
    file_path: str,
    *,
    max_rows: int = 10,
    start_line: int = 1,
    max_lines: int = 50,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Inspect and sample safe uploaded CSV, TSV, JSON, text, Markdown, or log files.

    Access stays inside the dedicated SANDBOX_ROOT_DIR, separate from source, configuration,
    and the working directory. Temporary analysis never modifies permanent Qdrant knowledge.
    """
    try:
        base_dir = Path(settings.SANDBOX_ROOT_DIR).resolve()
        if user_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}", user_id) or user_id in {
                ".",
                "..",
            }:
                raise FileSandboxSecurityError("Invalid account file boundary")
            user_entry = base_dir / user_id
            user_root = user_entry.resolve()
            if user_root != user_entry or not user_root.is_relative_to(base_dir):
                raise FileSandboxSecurityError("Account upload folder escapes the sandbox")
            base_dir = user_root
        result = inspect_file_content(
            file_path,
            base_dir=base_dir,
            max_rows=max_rows,
            start_line=start_line,
            max_lines=max_lines,
        )
        if len(json.dumps(result, ensure_ascii=False, default=str)) > 16_000:
            return {
                "error": "File sample exceeds the context budget; use a smaller file or narrower sample"
            }
        return result
    except FileSandboxSecurityError as exc:
        return {"error": f"Security violation: {exc}"}
    except FileNotFoundError as exc:
        return {"error": f"File not found: {exc}"}
    except Exception as exc:
        return {"error": f"Inspection error: {exc}"}
