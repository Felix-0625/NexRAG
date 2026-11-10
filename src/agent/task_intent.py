"""Conservatively filter clauses for supported completion contracts.

Remove explicit negations only during intent recognition; the original question remains
model input and the basis for numeric and source grounding.
"""

import re

NEGATIVE = r"不要|无需|不必|禁止|勿|不需要|不(?=检索|读取|查询|使用|调用|访问|导出|计算|联网|上网)"


def knowledge_only_request(prompt: str) -> bool:
    """Honor document-only instructions and explicit SQL prohibitions.

    Metric definitions can mention counts or totals without requesting current values. This
    flag never derives or relaxes database filters.
    """
    # A negated document clause cannot complete an earlier positive clause.
    # Public research that forbids knowledge retrieval is not a document-only task.
    affirmative_clauses = positive_request_text(prompt).split("，")
    return bool(
        any(
            re.search(
                r"(?:仅|只)(?:需要)?(?:检索|查阅|查|读取).{0,80}(?:文档|文件|知识库|制度|手册)",
                clause,
            )
            for clause in affirmative_clauses
        )
        and re.search(rf"(?:{NEGATIVE})(?:查询|查|访问|读取)?(?:业务)?数据库", prompt)
    )


def positive_request_text(prompt: str) -> str:
    """Split clauses and contrast words, discard negated clauses, and rejoin positive requests."""
    # Negation may follow a request without punctuation.
    # Contrast words separate later positive requests from prohibitions.
    clauses = re.split(
        rf"[，,。！？!?；;\n]|(?:但是|但|而是|然后)|(?=(?:{NEGATIVE}))|(?=只(?:需要)?(?:查询|检索|读取|查|读|用))",
        prompt,
    )
    return "，".join(
        clause
        for clause in clauses
        if not re.match(rf"^\s*(?:(?:也|请)\s*)?(?:{NEGATIVE})", clause)
    )


def prohibited_tools(prompt: str) -> set[str]:
    """Honor explicit tool-family prohibitions without changing ACLs.

    This bounded matcher supplements independent data authorization and tool approval checks.
    """
    clauses = re.split(r"[，,。！？!?；;\n]|(?:但是|但|而是|然后)", prompt)
    blocked = set()
    for clause in clauses:
        match = re.search(NEGATIVE, clause)
        if match is None:
            continue
        negative = clause[match.start() :]
        for pattern, names in (
            (
                r"数据库|业务数据|\bSQL\b",
                {"get_business_schema", "query_database", "query_business_metrics"},
            ),
            (r"知识库|文档|附件|\bKB\b", {"search_knowledge_documents"}),
            (r"联网|上网|外部搜索|网页搜索", {"search_web_engine"}),
            (r"导出|下载|\bexport\b", {"export_analysis"}),
            (r"本地文件|本机文件|文件系统", {"inspect_local_file"}),
        ):
            if re.search(pattern, negative, re.I):
                blocked |= names
    return blocked


def plan_prohibition_conflicts(prompt: str, capabilities: set[str]) -> set[str]:
    """Reject model-written plan steps that contradict the user's tool restrictions.

    Do not remove existing requirements or grant access; reject impossible new obligations
    before changing plan state.
    """
    blocked = prohibited_tools(prompt)
    conflicts = set()
    for capability in capabilities:
        if capability == "kb" and "search_knowledge_documents" in blocked:
            conflicts.add(capability)
        elif capability == "web" and "search_web_engine" in blocked:
            conflicts.add(capability)
        elif capability == "export" and "export_analysis" in blocked:
            conflicts.add(capability)
        elif (capability == "db" or capability.startswith(("db_", "metric:"))) and (
            "query_database" in blocked
        ):
            conflicts.add(capability)
    return conflicts


def database_write_request(prompt: str) -> bool:
    """Reject explicit business-write requests before calling the model.

    This is early feedback only; SQL and connection guards remain authoritative.
    """
    if re.search(
        r"\b(?:update\s+\w+\s+set|delete\s+from|insert\s+into|drop\s+table|alter\s+table|truncate\s+table)\b",
        prompt,
        re.I,
    ):
        return True
    # Match imperative writes, not descriptive nouns such as update identifiers.
    # SQL AST validation and read-only source credentials remain authoritative regardless of this shortcut.
    clauses = re.split(r"[，,。！？!?；;\n]|(?:然后|并且|并)", prompt)
    for clause in clauses:
        if not re.search(r"数据库|业务数据|订单|销售|库存|发票", clause):
            continue
        action = re.sub(r"^\s*(?:请)?(?:帮我|替我)?(?:直接|立即|先|再)?\s*", "", clause)
        if re.match(r"(?:把|将).{0,100}(?:改为|改成|删除|清空|更新为|设置为)", action):
            return True
        if re.match(
            r"(?:修改|更改|删除|清空|写入|插入|更新)(?!标识|时间|日志|说明|历史).{0,60}"
            r"(?:金额|记录|订单|数据|库存|发票|销售)",
            action,
        ):
            return True
        if re.match(
            r"在.{0,40}(?:数据库|业务数据).{0,10}(?:修改|删除|清空|写入|插入|更新)", action
        ):
            return True
    return False


def _database_capabilities(
    normalized_prompt: str, business_text: str, has_business: bool
) -> set[str]:
    """Recognize database capabilities and their detail, count, and total variants."""
    required = set()
    # ASCII identifiers can touch CJK text, where Unicode word boundaries would miss them.
    if re.search(
        r"(?<![a-z0-9_])(?:select|sql|db)(?![a-z0-9_])|数据库|业务数据|订单号|order_id",
        business_text,
    ):
        required.add("db")
    elif re.search(
        r"销售|订单|营收|营业额|收入|库存|发票|sales|orders|revenue|inventory|invoices",
        business_text,
    ) and re.search(
        r"查询|查出|查一下|查(?:订单|业务|销售|库存|发票)|逐笔|明细|统计|合计|总额|总数|总金额|多少|计算|列出|本月|今年|how many|count|total|sum|list|query",
        business_text,
    ):
        required.add("db")
    elif has_business and re.search(
        r"总金额|总笔数|总订单|统计|how many|count my|sum of|total amount", normalized_prompt
    ):
        required.add("db")
    if "db" in required and re.search(
        r"明细|逐笔|订单号|全部订单(?!的?(?:总金额|总额|金额合计|数量|订单数))|order_id|order details|individual orders",
        normalized_prompt,
    ):
        required.add("db_details")
    if "db" in required and re.search(r"订单数|笔数|多少.*订单|count|how many", normalized_prompt):
        required.add("db_count")
    if "db" in required and re.search(
        r"总金额|金额合计|总额|总收入|total amount|sum\(", normalized_prompt
    ):
        required.add("db_total")
    return required


_WEB_REQUEST = re.compile(
    r"联网搜索|上网搜索|网页搜索|外部搜索|互联网搜索|搜索引擎|search_web_engine|web search|search the web"
)


def _knowledge_text(normalized_prompt: str) -> str:
    """Remove web-only and tool-approval clauses before determining knowledge obligations."""
    # Tool-approval administration is not retrieval of business approval policies.
    # Web-search clauses do not imply local knowledge needs; separate knowledge clauses still do.
    knowledge_text = "，".join(
        clause
        for clause in normalized_prompt.split("，")
        if not (
            _WEB_REQUEST.search(clause)
            and not re.search(r"知识库|本地|已授权|私人|企业制度|附件|上传", clause)
        )
    )
    return re.sub(r"(?:工具|MCP)\s*(?:已)?(?:撤销|撤回)?审批", "", knowledge_text, flags=re.I)


def requested_capabilities(prompt: str, has_business: bool) -> set[str]:
    """Derive conservative local completion contracts for supported tasks.

    Enforce explicit wording here; evaluation must cover open-ended intent. Models may add
    requirements but cannot remove these obligations.
    """
    positive_prompt = positive_request_text(prompt)
    normalized_prompt = positive_prompt.lower()
    business_text = re.sub(r"(?:企业)?销售制度|sales polic(?:y|ies)", "制度", normalized_prompt)
    required = _database_capabilities(normalized_prompt, business_text, has_business)
    knowledge_text = _knowledge_text(normalized_prompt)
    if re.search(
        r"知识库|文档|制度|手册|私有项目|项目预算|审批|折扣上限|附件|上传.*文件|knowledge|document|policy|manual|attachment|(?<![a-z0-9_])kb(?![a-z0-9_])",
        knowledge_text,
    ):
        required.add("kb")
    if re.search(
        r"导出|下载.*(?:报告|表格|csv)|(?:报告|表格|文件).{0,20}供.{0,8}下载|export|download.*(?:report|csv)",
        normalized_prompt,
    ):
        required.add("export")
    if _WEB_REQUEST.search(normalized_prompt):
        required.add("web")
    from src.agent.derived_calculation import CALCULATION, requested_discount

    if requested_discount(positive_prompt):
        required.add(CALCULATION)
    if knowledge_only_request(prompt):
        required -= {"db", "db_details", "db_count", "db_total"}
        required.add("kb")
    if "kb" in required and "db" not in required and CALCULATION not in required:
        from src.agent.arithmetic_contract import requested_arithmetic

        required |= requested_arithmetic(positive_prompt)
    return required
