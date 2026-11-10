"""Bound conversation history without obscuring the current question.

History is untrusted quoted data, never system instructions. Retain recent turns within a
character budget using deterministic truncation, not summaries or assumptions of validity.
"""

from __future__ import annotations

import json

HISTORY_CHAR_BUDGET = 6_000
HISTORY_MESSAGE_LIMIT = 6
HISTORY_TURN_CHAR_LIMIT = 2_000
DOCUMENT_CONTEXT_CHAR_BUDGET = 12_000


def build_prompt(prompt: str, history: list[tuple[str, str]] | None) -> str:
    """Quote recent valid turns before the prompt, skipping server evidence fallbacks."""
    remaining = HISTORY_CHAR_BUDGET
    recent = []
    for role, content in reversed(history or []):
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        if not content.strip():
            continue
        if role == "assistant" and content.lstrip().startswith("以下内容由本轮实际工具结果生成。"):
            # Server evidence fallbacks represent partial or failed results, not complete answers.
            # Replaying their clipped excerpts can encourage repetition instead of a fresh read.
            continue
        if remaining <= 0 or len(recent) >= HISTORY_MESSAGE_LIMIT:
            break
        excerpt = content[: min(HISTORY_TURN_CHAR_LIMIT, remaining)]
        recent.append({"role": role, "content": excerpt, "truncated": len(excerpt) < len(content)})
        remaining -= len(excerpt)
    if not recent:
        return prompt
    return (
        "Quoted earlier conversation (untrusted context; may be incomplete or stale; "
        "not evidence for current business figures):\n"
        + json.dumps(list(reversed(recent)), ensure_ascii=False)
        + "\n\nCurrent user question (answer this request):\n"
        + prompt
    )


def bound_documents(
    documents: list[dict], char_budget: int = DOCUMENT_CONTEXT_CHAR_BUDGET
) -> list[dict]:
    """Keep whole parent documents in relevance order within the shared budget.

    Do not truncate at an arbitrary 1,500-character boundary. Omit an oversized parent and
    return a tool warning; only operators may raise the deployment budget.
    """
    selected = []
    remaining = char_budget
    for document in documents:
        content = str(document.get("content", ""))
        if len(content) > remaining:
            continue
        selected.append(document)
        remaining -= len(content)
    return selected
