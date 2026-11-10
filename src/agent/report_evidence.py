"""Keep original policy text visible in mixed business and knowledge reports.

Even correctly cited model prose can omit requested approvers, versions, or rates. Append
source text deterministically after validation, using only authorized budgeted retrieval,
without another model extracting or rewriting facts.
"""


def append_report_sources(answer: str, ledger) -> str:
    """Append retrieved policy quotes, or just sources for knowledge-only requests; preserve
    answers without documents.
    """
    if not {"db", "kb"} <= ledger.required:
        return append_knowledge_sources(answer, ledger)
    sections = []
    seen = set()
    for entry in ledger.entries:
        if entry["kind"] != "kb":
            continue
        for document in entry["data"]:
            content = str(document.get("content", "")).strip()
            source = str(document.get("source", "知识文档"))
            identity = (source, content)
            if not content or identity in seen:
                continue
            seen.add(identity)
            sections.append(
                f"[{entry['id']}] {source}\n\n"
                + "\n".join("> " + line for line in content.splitlines())
            )
    if not sections:
        return answer
    return answer + "\n\n**本轮文档原文依据**\n\n" + "\n\n".join(sections)


def append_knowledge_sources(answer: str, ledger) -> str:
    """Keep retrieved source identities visible even if the answer omits citations.

    This inventory does not claim sentence-level entailment. Include only authorized nonempty
    documents and escape source names as inert text, not generated URLs.
    """
    import html
    import re

    sources = []
    seen = set()
    for entry in ledger.entries:
        if entry["kind"] != "kb":
            continue
        for document in entry["data"]:
            source = " ".join(str(document.get("source", "")).split())
            if not source or not str(document.get("content", "")).strip():
                continue
            if source in seen:
                continue
            seen.add(source)
            safe_source = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(source))
            sources.append(f"- [{entry['id']}] {safe_source}")
    if not sources:
        return answer
    return answer + "\n\n**本轮检索来源**\n\n" + "\n".join(sources)
