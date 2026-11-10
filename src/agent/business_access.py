"""Provide authoritative denial evidence for requests naming another business scope.

The agent cannot bypass SQL evidence by claiming denial. Compare exact requested scopes
with current grants without switching scope or reading business rows.
"""

import re

from src.agent.task_intent import positive_request_text
from src.midplatform.tenant_access import Principal, allowed_tenants, list_tenants


def explicit_scope_refusal(deps) -> dict | None:
    """Resolve explicit catalog IDs before spending a model call on refusal.

    Do not guess names or grant access; unknown IDs remain subject to sandbox and access checks.
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
    """Check an explicit scope ID against current grants and selection without reading business
    rows.

    Return errors for invalid or unrequested IDs; record business_denial for inaccessible
    scopes.
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
