"""为受支持的完成契约做保守的分句过滤。

这里只在任务识别时剔除显式的否定指令；原始问题仍是模型输入，也是数字/来源证据的依据。
"""

import re

NEGATIVE = r"不要|无需|不必|禁止|勿|不需要|不(?=检索|读取|查询|使用|调用|访问|导出|计算|联网|上网)"


def knowledge_only_request(prompt: str) -> bool:
    """遵从显式的“仅文档”指令以及对 SQL 的禁止。

    指标定义可能提到计数/合计，但并不是在请求其当前值。此标志从不推导或放宽数据库过滤条件。
    """
    # 否定的文档分句不能补全前面的肯定分句：
    # "只查询公开主题，不读取知识库" 属于公开调研，不是仅文档任务。
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
    """按分句与转折词拆分请求，丢弃以否定词开头的分句，用“，”拼回肯定部分。"""
    # 否定尾巴可能没有标点，如 "查订单不要知识库"；
    # 转折词把后面的肯定请求从禁止语句中重新拆出来。
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
    """遵从显式的工具族否定指令，但不改动 ACL。

    这是有边界的分句匹配器；即使没有识别出显式禁止，数据授权和工具审批也会独立重新检查。
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
    """拒绝与用户工具限制相矛盾的、由模型编写的计划步骤。

    这既不会移除用户已有的完成要求，也不会授予访问权限；它是在计划状态变化之前，防止产生不可能满足的新义务。
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
    """在调用模型之前，拒绝显式的业务写入请求。

    这只是为了更早给出反馈；真正具有权威性的仍是 SQL 与连接层的防护。
    """
    if re.search(
        r"\b(?:update\s+\w+\s+set|delete\s+from|insert\s+into|drop\s+table|alter\s+table|truncate\s+table)\b",
        prompt,
        re.I,
    ):
        return True
    # 这个反馈捷径只匹配祈使分句，不匹配 "更新标识" 之类的描述性名词；
    # 即使没有命中任何自然语言捷径，SQL AST 校验和数据源只读凭据依然是权威。
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


def _database_capabilities(p: str, business_text: str, has_business: bool) -> set[str]:
    """识别 db 及其细分能力（db_details / db_count / db_total）。"""
    required = set()
    # ASCII 标识符可能紧贴中文词；Unicode 的 \b 会漏掉 "查DB"。
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
        r"总金额|总笔数|总订单|统计|how many|count my|sum of|total amount", p
    ):
        required.add("db")
    if "db" in required and re.search(
        r"明细|逐笔|订单号|全部订单(?!的?(?:总金额|总额|金额合计|数量|订单数))|order_id|order details|individual orders",
        p,
    ):
        required.add("db_details")
    if "db" in required and re.search(r"订单数|笔数|多少.*订单|count|how many", p):
        required.add("db_count")
    if "db" in required and re.search(r"总金额|金额合计|总额|总收入|total amount|sum\(", p):
        required.add("db_total")
    return required


_WEB_REQUEST = re.compile(
    r"联网搜索|上网搜索|网页搜索|外部搜索|互联网搜索|搜索引擎|search_web_engine|web search|search the web"
)


def _knowledge_text(p: str) -> str:
    """去掉仅属于联网搜索的分句与工具审批措辞后，供知识库义务判断使用的文本。"""
    # 工具审批管理并不是在检索业务审批制度；其他审批类问题仍须有知识库证据。
    # 提到公开文档的联网搜索分句本身不是本地知识库请求；单独的知识库分句仍保留其义务。
    knowledge_text = "，".join(
        clause
        for clause in p.split("，")
        if not (
            _WEB_REQUEST.search(clause)
            and not re.search(r"知识库|本地|已授权|私人|企业制度|附件|上传", clause)
        )
    )
    return re.sub(r"(?:工具|MCP)\s*(?:已)?(?:撤销|撤回)?审批", "", knowledge_text, flags=re.I)


def requested_capabilities(prompt: str, has_business: bool) -> set[str]:
    """为应用支持的任务提供保守的本地任务契约。

    显式的措辞可在此强制执行；开放式的语义意图仍需靠评测覆盖。模型可以增加要求，但不能移除这些要求。
    """
    positive_prompt = positive_request_text(prompt)
    p = positive_prompt.lower()
    business_text = re.sub(r"(?:企业)?销售制度|sales polic(?:y|ies)", "制度", p)
    required = _database_capabilities(p, business_text, has_business)
    knowledge_text = _knowledge_text(p)
    if re.search(
        r"知识库|文档|制度|手册|私有项目|项目预算|审批|折扣上限|附件|上传.*文件|knowledge|document|policy|manual|attachment|(?<![a-z0-9_])kb(?![a-z0-9_])",
        knowledge_text,
    ):
        required.add("kb")
    if re.search(
        r"导出|下载.*(?:报告|表格|csv)|(?:报告|表格|文件).{0,20}供.{0,8}下载|export|download.*(?:report|csv)",
        p,
    ):
        required.add("export")
    if _WEB_REQUEST.search(p):
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
