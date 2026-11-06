"""在业务/知识混合报告中保持制度原文可见。

模型行文可能漏掉被明确要求的审批人、版本标记或比例，即使引用了正确的文档。
因此在通过校验的正文之后，确定性地渲染来源原文，不让另一个模型去抽取或改写这些事实；
只使用本轮已授权且受预算限制的检索结果。
"""


def append_report_sources(answer: str, ledger) -> str:
    """在答案后附上本轮检索到的知识库原文引用；仅有知识库需求时只附来源清单，没有可用文档则原样返回。"""
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
    """即使正文省略了引用，也让检索到的来源身份保持可见。

    这是检索来源清单，并不声称每句话都由来源推出。只列出本轮已授权的非空文档；
    来源名称保持为转义后的惰性文本，而不是生成的 URL。
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
