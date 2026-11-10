"""Resolve a document from authorized candidates before searching its evidence."""

import re


def document_identity(meta: dict) -> tuple[str, str, str] | None:
    scope, file_id, version = (
        meta.get("collection_name"),
        meta.get("file_id"),
        meta.get("ingest_version"),
    )
    if (
        isinstance(scope, str)
        and scope
        and isinstance(file_id, str)
        and file_id
        and isinstance(version, str)
        and version
    ):
        return scope, file_id, version
    return None


def consensus_document(metadatas: list[dict]) -> tuple[str, str, str] | None:
    if len(metadatas) < 3:
        return None
    document = document_identity(metadatas[0])
    if document is not None and all(
        document_identity(meta) == document for meta in metadatas[1:3]
    ):
        return document
    return None


def scoped_evidence_query(
    query: str, metadatas: list[dict]
) -> tuple[tuple[str, str, str], str] | None:
    """Split source context from a question only when the leading hits agree on its version."""
    parts = [part.strip() for part in re.split(r"[;\uFF1B\r\n]+", query) if part.strip()]
    question_marks = ("?", "\uff1f")
    if len(parts) != 2 or not parts[1].endswith(question_marks):
        return None
    if any(mark in parts[0] for mark in question_marks):
        return None
    document = consensus_document(metadatas)
    return (document, parts[1]) if document is not None else None
