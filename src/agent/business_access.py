"""为指名其他业务范围的请求提供权威的拒绝证据。

Agent 不能自行声称“无权限”来绕过 SQL 证据。本工具把用户提到的确切范围与当前授权比对，
既不会更改已选范围，也不会读取业务行。
"""

import re

from src.agent.task_intent import positive_request_text
from src.midplatform.tenant_access import Principal, allowed_tenants, list_tenants


def explicit_scope_refusal(deps) -> dict | None:
    """在为拒绝消耗一次模型调用之前，先解析显式出现的目录 ID。

    不会猜测文本中的名称；未知 ID 仍受同一查询沙箱和访问检查工具约束。本检查从不授予访问权限。
    """
    if "db" not in deps.ledger.required:
        return None
    catalog = deps.evaluation.authorized_tenants if deps.evaluation is not None else list_tenants()
    positive = positive_request_text(deps.ledger.prompt)
    for scope_id in catalog:
        if scope_id == deps.context.tenant_id:
            continue
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(scope_id)}(?![A-Za-z0-9_-])", positive):
            result = check_requested_scope(deps, scope_id)
            if result.get("allowed") is False:
                return result
    return None


def check_requested_scope(deps, scope_id: str) -> dict:
    """核对用户明确提到的业务范围 ID 与当前授权和选定范围。

    ID 格式非法或未出现在正向请求文本中则返回 error；未授权或非选定范围时登记 business_denial 证据并返回拒绝，不读取任何业务行。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope_id):
        return {"error": "Use an exact business scope ID explicitly requested by the user"}
    positive = positive_request_text(deps.ledger.prompt)
    if not re.search(rf"(?<![A-Za-z0-9_-]){re.escape(scope_id)}(?![A-Za-z0-9_-])", positive):
        return {"error": "Cannot certify a refusal for a scope absent from the positive request"}
    context = deps.context
    allowed = (
        deps.evaluation.authorized_tenants
        if deps.evaluation is not None
        else allowed_tenants(Principal(context.user_id, context.email, context.role))
    )
    if scope_id in allowed and scope_id == context.tenant_id:
        return {"allowed": True, "selected": True, "instruction": "Query the authorized view"}
    message = (
        "该业务范围未授予当前账号，已拒绝读取；没有查询或返回该范围的数据。"
        if scope_id not in allowed
        else "该业务范围不是当前选定范围。请先在业务数据范围中切换，再执行查询。"
    )
    evidence_id = deps.ledger.add("business_denial", {"scope_id": scope_id, "message": message})
    if deps.evaluation is not None:
        deps.evaluation.denied = True
    return {"allowed": False, "message": message, "evidence_id": evidence_id}
