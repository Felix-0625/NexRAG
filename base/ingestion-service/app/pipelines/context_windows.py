"""Store source-bound windows using the indexed-neighborhood retrieval pattern.

Like LlamaIndex's SentenceWindowNodeParser, only the small leaf is embedded;
the original-text window remains metadata until retrieval. Offsets refer to
the extracted source text, not byte offsets in a PDF or Office container.
"""

import hashlib
import logging

from haystack import Document

log = logging.getLogger(__name__)


def attach_context_windows(source: Document, leaves: list[Document], radius: int) -> None:
    """Locate ordered leaves inside their parents, then attach exact source slices.

    Work on one extracted source at a time so a window cannot cross a file,
    collection or ingest version. Never guess offsets for transformed text.
    A leaf that cannot be located keeps its original content without a window.
    """
    text = source.content or ""
    source_hash = hashlib.sha256(text.encode()).hexdigest()
    positions: list[tuple[int, int] | None] = []
    parent_id = None
    parent_cursor = 0
    parent_end = 0
    parent_start = -1
    child_cursor = 0
    child_end = 0
    for leaf in leaves:
        meta = leaf.meta
        for field in (
            "context_window",
            "context_window_start",
            "context_window_end",
            "source_text_sha256",
            "source_id",
        ):
            meta.pop(field, None)
        parent = meta["parent_text"]
        if meta["parent_id"] != parent_id:
            parent_id = meta["parent_id"]
            # Every ordered chunk introduces source characters beyond its
            # predecessor, even when they overlap. Repeated signatures must
            # not match an occurrence already covered by the previous chunk.
            parent_start = text.find(parent, max(parent_cursor, parent_end - len(parent) + 1))
            if parent_start >= 0:
                parent_cursor = parent_start + 1
                parent_end = parent_start + len(parent)
            child_cursor = 0
            child_end = 0
        content = leaf.content or ""
        child_start = (
            parent.find(content, max(child_cursor, child_end - len(content) + 1))
            if content
            else -1
        )
        # A Haystack word splitter may carry parent-relative offsets. Remove
        # them before publishing only offsets verified against this source.
        meta.pop("split_idx_start", None)
        if parent_start < 0 or child_start < 0:
            positions.append(None)
            continue
        child_cursor = child_start + 1
        child_end = child_start + len(content)
        start = parent_start + child_start
        positions.append((start, start + len(content)))
        meta.update(source_id=source.id, source_text_sha256=source_hash, split_idx_start=start)

    missing = 0
    for index, leaf in enumerate(leaves):
        neighbors = positions[max(0, index - radius) : index + radius + 1]
        if any(position is None for position in neighbors):
            missing += 1
            continue
        valid = [position for position in neighbors if position is not None]
        start = min(position[0] for position in valid)
        end = max(position[1] for position in valid)
        leaf.meta.update(
            context_window=text[start:end], context_window_start=start, context_window_end=end
        )
    if missing:
        log.warning(
            "Context windows unavailable for %d leaves with unverified source offsets", missing
        )
