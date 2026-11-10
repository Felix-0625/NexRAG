"""Bind explicit business deliverables to the operator-owned semantic catalog.

Names and aliases belong to the catalog, not the model. This bounded resolver leaves unknown
paraphrases under the normal evidence contract; evaluation must cover those variants.
"""

from __future__ import annotations

import re
import unicodedata

from src.agent.task_intent import positive_request_text


def _named(text: str, name: str) -> bool:
    name = unicodedata.normalize("NFKC", name).casefold().strip()
    if not name:
        return False
    if name.isascii():
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text))
    return name in text


def catalog_requirements(prompt: str, definitions: dict, required: set[str]) -> set[str]:
    """Resolve each requested metric independently so one entity cannot satisfy another."""
    if "db" not in required:
        return set()
    text = unicodedata.normalize("NFKC", positive_request_text(prompt)).casefold()
    matched = {
        identifier: spec
        for identifier, spec in definitions.items()
        if isinstance(spec, dict)
        and any(
            _named(text, str(name))
            for name in [identifier, spec.get("description", ""), *spec.get("aliases", [])]
        )
    }
    obligations = {"metric:" + identifier for identifier in matched}
    # Explicit joins require one certified joined result, not unrelated single-view queries.
    # Only catalog names are authoritative.
    tables = {spec["table"] for spec in matched.values() if spec.get("table")}
    if (
        "db_details" in required
        and len(tables) > 1
        and any(word in text for word in ("关联", "联查", "join"))
    ):
        obligations.add("db_join:" + ",".join(sorted(tables)))
        # Complete joined details must include declared metric inputs alongside detail rows.
        # Validate results without adding queries, rewriting projections, or dropping user filters.
        import sqlglot
        from sqlglot import exp

        for spec in matched.values():
            if spec.get("kind") == "count":
                # A joined entity key may come from either side; it is not an amount-detail field.
                continue
            expression = sqlglot.parse_one(spec["calculation"], read="sqlite")
            obligations |= {
                f"db_field:{spec['table']}.{column.name}".casefold()
                for column in expression.find_all(exp.Column)
            }
    return obligations


def bind_catalog_requirements(ledger, evaluation=None) -> None:
    """Bind requirements before execution using the production metric-tool catalog."""
    if "db" not in ledger.required:
        return
    if evaluation is not None:
        definitions = evaluation.business_metrics or {}
    else:
        from src.config import settings
        from src.midplatform.catalog import MetadataCatalog
        from src.midplatform.workspace import active_connection

        active = active_connection()
        definitions = MetadataCatalog(
            active["metadata_path"] if active else settings.METADATA_PATH
        ).metadata.get("metrics", {})
    ledger.registered_metric_ids = frozenset(definitions)
    ledger.required |= catalog_requirements(ledger.prompt, definitions, ledger.required)


def completed_catalog_requirements(entries, required=()) -> set[str]:
    """Accept only real typed metrics and certified joins as completed catalog obligations."""
    import sqlglot
    from sqlglot import exp

    from src.midplatform.query_semantics import evidence_shape

    completed = {
        "metric:" + entry["data"]["metric_id"] for entry in entries if entry["kind"] == "metric"
    }
    for entry in entries:
        if entry["kind"] != "db":
            continue
        shape = evidence_shape(entry)
        if not (
            shape.get("row_preserving")
            and not shape.get("limited")
            and shape.get("join_cardinality")
        ):
            continue
        try:
            query = sqlglot.parse_one(entry.get("sql", ""), read="sqlite")
        except (ValueError, sqlglot.errors.ParseError):
            continue
        tables = sorted({table.name for table in query.find_all(exp.Table)})
        # Require fields in the same joined result; unrelated ledger entries cannot repair incomplete details.
        fields = projected_detail_fields(query, entry["data"])
        field_requirements = {
            item.removeprefix("db_field:") for item in required if item.startswith("db_field:")
        }
        completed |= {"db_field:" + name for name in fields}
        if field_requirements <= fields:
            completed.add("db_join:" + ",".join(tables))
    return completed


def projected_detail_fields(query, rows) -> set[str]:
    """Parse directly selected source columns and aliases with SQLGlot.

    Expressions such as amount*0 and unrelated constants cannot replace source columns;
    ambiguous unqualified columns remain uncertified.
    """
    from sqlglot import exp

    aliases = {
        table.alias_or_name.casefold(): table.name.casefold() for table in query.find_all(exp.Table)
    }
    keys = {str(key).casefold() for key in rows[0]} if rows else None
    fields = set()
    for projection in query.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(node, exp.Column) or node.is_star:
            continue
        if keys is not None and projection.alias_or_name.casefold() not in keys:
            continue
        table = aliases.get(node.table.casefold()) if node.table else None
        if table is None and len(aliases) == 1:
            table = next(iter(aliases.values()))
        if table:
            fields.add(f"{table}.{node.name.casefold()}")
    return fields
