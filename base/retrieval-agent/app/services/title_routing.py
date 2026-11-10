"""Apply a bounded prior for explicit source-title hints, reordering only authorized candidates."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from urllib.parse import unquote

# Stop words only affect the strength of the title hint. Vague hints keep the original order.
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "by",
        "in",
        "on",
        "at",
        "from",
        "with",
        "is",
        "are",
        "was",
        "were",
        "does",
        "do",
        "it",
        "that",
        "this",
        "document",
        "agreement",
        "non",
        "disclosure",
        "consider",
        "indicate",
        "state",
        "between",
        "party",
        "receiving",
        "information",
        "confidential",
        "confidentiality",
        "nda",
        "template",
        "form",
        "standard",
        "executed",
        "inc",
        "ltd",
        "llc",
        "limited",
        "pdf",
        "txt",
        "mutual",
        "use",
        "two",
        "one",
        "draft",
        "rev",
        "english",
        "new",
        "sample",
        "blank",
        "en",
        "co",
        "公司",
        "协议",
        "文档",
        "条款",
        "请问",
        "说明",
        "文件",
        "哪个",
        "是否",
        "合同",
        "有关",
        "关于",
        "根据",
        "什么",
    ]
)


def _terms(text: str) -> set[str]:
    if not isinstance(text, str):
        return set()
    try:
        text = unicodedata.normalize("NFKC", unquote(text, errors="strict")).casefold()
    except UnicodeError:
        return set()
    terms = set(re.findall(r"[a-z][a-z0-9]{1,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return terms - _STOP


def explicit_title_order(query: str, metadatas: list[dict]) -> list[int]:
    """Prefer the matching source when title evidence is unique and strong.

    Candidates are unchanged.

    Counts cover only the current candidates. Library, file and ingestion version together
    identify a source. If the same title appears in several sources or the match is weak,
    the model ranking is kept as is.
    """
    original = list(range(len(metadatas)))
    documents: dict[tuple, set[str]] = {}
    identities = []
    for meta in metadatas:
        identity = (meta.get("collection_name"), meta.get("file_id"), meta.get("ingest_version"))
        identities.append(identity)
        if not identity[0] or not identity[1]:
            continue
        terms = _terms(meta.get("search_title", ""))
        if terms:
            documents.setdefault(identity, set()).update(terms)
    if len(documents) < 2:
        return original
    query_terms = _terms(query)
    frequency = Counter(term for terms in documents.values() for term in terms)
    scores = {
        identity: sum(
            math.log(1 + len(documents) / frequency[term]) for term in terms & query_terms
        )
        for identity, terms in documents.items()
    }
    ranked = sorted(scores, key=lambda identity: scores[identity], reverse=True)
    best, second = scores[ranked[0]], scores[ranked[1]]
    if best < 3 or best - second < 1.5:
        return original
    preferred = ranked[0]
    return sorted(original, key=lambda index: identities[index] != preferred)
