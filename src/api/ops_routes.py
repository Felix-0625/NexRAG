"""仅限管理员的运维 API：trace、数据集、实验、提案。

所有接口都要求共享服务密钥、经签名的 Open WebUI 身份以及 role=admin。
trace 只保存脱敏内容和哈希后的用户 ID。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from src.evaluation import datasets
from src.evolution.artifacts import registry
from src.evolution.cycle import EvolutionCycle
from src.evolution.governance import Governance, GovernanceError
from src.midplatform import knowledge_library
from src.midplatform.ops_store import get_ops_store
from src.midplatform.tenant_access import Principal, get_principal
from src.observability.tracer import tracer

router = APIRouter(prefix="/ops", tags=["ops"])


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    """依赖项：要求已验证身份的角色为 admin，否则返回 403；通过则返回该身份。"""
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return principal


@router.get("/overview")
def overview(days: float = Query(7, gt=0, le=90), _: Principal = Depends(require_admin)):
    """管理员查看最近若干天（最多 90）的运行概览：trace 总量与时延、评分均值、按日趋势、提案积压和线上产物版本。"""
    store = get_ops_store()
    since = __import__("time").time() - days * 86_400
    totals = (
        store.query_one(
            "SELECT COUNT(*) AS traces, AVG(latency_ms) AS avg_latency, "
            "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, SUM(canary) AS canary "
            "FROM traces WHERE created_at >= ?",
            (since,),
        )
        or {}
    )
    score_means = store.query(
        "SELECT s.name, s.source, AVG(s.value) AS mean, COUNT(*) AS n FROM scores s JOIN traces t "
        "ON t.id = s.trace_id WHERE t.created_at >= ? GROUP BY s.name, s.source",
        (since,),
    )
    daily = store.query(
        "SELECT CAST(created_at / 86400 AS INTEGER) * 86400 AS day, COUNT(*) AS traces, "
        "AVG(latency_ms) AS avg_latency, AVG(CASE WHEN status = 'ok' THEN 1.0 ELSE 0 END) AS success "
        "FROM traces WHERE created_at >= ? GROUP BY day ORDER BY day",
        (since,),
    )
    return {
        "totals": totals,
        "scores": score_means,
        "daily": daily,
        "backlog": Governance().backlog_status(),
        "production": {k: registry.production(k).id for k in ("prompt", "params")},
    }


@router.get("/traces")
def list_traces(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = None,
    principal: Principal = Depends(require_admin),
):
    """管理员列出最近的 trace，可按状态过滤；只有管理员自己的 trace 带输入预览，他人的不返回内容。"""
    clause, params = ("WHERE status = ?", (status,)) if status else ("", ())
    from src.observability.redaction import hash_user

    return get_ops_store().query(
        f"SELECT id, user_hash, tenant_id, chat_id, status, latency_ms, canary, created_at, "
        f"CASE WHEN user_hash=? THEN substr(input_text, 1, 160) ELSE NULL END AS input_preview FROM traces {clause} ORDER BY created_at DESC LIMIT ?",
        (hash_user(principal.user_id), *params, limit),
    )


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, principal: Principal = Depends(require_admin)):
    """管理员查看单条 trace 的步骤与评分；不属于自己的 trace 会隐去输入、输出和错误详情，不存在返回 404。"""
    store = get_ops_store()
    trace = store.query_one("SELECT * FROM traces WHERE id = ?", (trace_id,))
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    from src.observability.redaction import hash_user

    if trace["user_hash"] != hash_user(principal.user_id):
        trace["input_text"] = trace["output_text"] = trace["error"] = None
    trace["artifact_versions"] = json.loads(trace["artifact_versions"])
    observations = store.query(
        "SELECT * FROM observations WHERE trace_id = ? ORDER BY position", (trace_id,)
    )
    if trace["user_hash"] != hash_user(principal.user_id):
        for item in observations:
            item["input_summary"] = item["output_summary"] = None
            item["error"] = "tool_execution_error" if item["error"] else None
    return {
        "trace": trace,
        "observations": observations,
        "scores": store.query(
            "SELECT * FROM scores WHERE trace_id = ? ORDER BY created_at", (trace_id,)
        ),
    }


class SyntheticSampleForm(BaseModel):
    input_text: str = Field(min_length=1, max_length=8_000)
    tenant_id: str = Field(pattern="^synthetic-[ab]$")
    split: str = Field(pattern="^(train|holdout|regression)$")
    expected: dict[str, Any]
    confirmed_synthetic: bool


@router.post("/datasets/synthetic")
def add_synthetic_sample(body: SyntheticSampleForm, _: Principal = Depends(require_admin)):
    """管理员向评估数据集加入一条人工构造的合成样本；必须确认其为合成数据且只用固定的合成租户，含个人标识或引用真实资源则返回 400。"""
    if not body.confirmed_synthetic or len(json.dumps(body.expected)) > 10_000:
        raise HTTPException(400, "请仅提交人工构造的合成样本和有限的预期结果")
    try:
        identifier = datasets.add_synthetic_item(
            body.input_text, body.tenant_id, body.split, body.expected
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": identifier}


class SyntheticExperimentForm(BaseModel):
    model_id: str | None = Field(default=None, max_length=150)
    repeats: int = Field(default=1, ge=1, le=3)


@router.post("/experiments/synthetic")
async def run_synthetic_samples(
    body: SyntheticExperimentForm, _: Principal = Depends(require_admin)
):
    """管理员用已登记的本机模型在隔离的合成环境中运行合成样本（不接触真实数据），重复 1 至 3 次并返回评估指标；选项无效返回 400。"""
    from src.evaluation.runner import run_synthetic_experiment

    try:
        result = await run_synthetic_experiment(model_id=body.model_id, repeats=body.repeats)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result.__dict__


@router.get("/datasets")
def list_dataset_items(split: str | None = None, _: Principal = Depends(require_admin)):
    """管理员列出评估数据集条目，可按 train、holdout、regression 划分过滤。"""
    splits = (split,) if split else datasets.SPLITS
    return [item.__dict__ for item in datasets.items(splits)]


@router.get("/proposals")
def list_proposals(status: str | None = None, _: Principal = Depends(require_admin)):
    """管理员列出演化提案（可按状态过滤）及审核积压状态。"""
    return {"proposals": Governance().list(status), "backlog": Governance().backlog_status()}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, _: Principal = Depends(require_admin)):
    """管理员查看单个提案详情；处于灰度中的提案附带实时灰度指标，不存在返回 404。"""
    proposal = Governance().get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    cycle = EvolutionCycle()
    if proposal["status"] == "canary":
        proposal["canary_metrics"] = cycle.canary_metrics(proposal)
    return proposal


class ReviewBody(BaseModel):
    note: str | None = Field(default=None, max_length=1_000)


def _act(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except GovernanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: str, body: ReviewBody, admin: Principal = Depends(require_admin)):
    """管理员批准提案，仅启动灰度而不直接上线；线上版本已变化则标记过期，状态不允许时返回 409。"""
    return _act(Governance().approve, proposal_id, admin.email, body.note)


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: str, body: ReviewBody, admin: Principal = Depends(require_admin)):
    """管理员驳回待审核或已过期的提案，并废弃其候选版本；状态不允许时返回 409。"""
    return _act(Governance().reject, proposal_id, admin.email, body.note)


@router.post("/proposals/{proposal_id}/promote")
def promote(proposal_id: str, admin: Principal = Depends(require_admin)):
    """管理员对灰度中的提案做第二次确认并晋升为线上版本；灰度天数、样本量或指标不达标时由服务端重新计算并拒绝（409）。"""
    return _act(Governance().promote, proposal_id, admin.email)


@router.post("/proposals/{proposal_id}/rollback")
def rollback(proposal_id: str, body: ReviewBody, admin: Principal = Depends(require_admin)):
    """管理员终止灰度，或把已晋升的版本回滚到前一版本；其他状态返回 409。"""
    return _act(Governance().rollback, proposal_id, body.note or "manual rollback", admin.email)


@router.post("/evolution/run")
async def run_cycle(_: Principal = Depends(require_admin)):
    """管理员手动触发一次自进化周期：监控灰度、挖掘失败聚类并生成和评估候选，返回周期报告；候选只进入待审核，不会直接上线。"""
    from src.memory.runtime import llm_complete

    report = await EvolutionCycle(completer=llm_complete).run()
    return report.__dict__


class FeedbackBody(BaseModel):
    trace_id: str = Field(min_length=1, max_length=80)
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1_000)


@router.post("/feedback")
def feedback(body: FeedbackBody, principal: Principal = Depends(get_principal)):
    """接收 Open WebUI 的点赞/点踩；任何已签名用户都只能评价自己的回答。"""
    from src.observability.redaction import hash_user

    store = get_ops_store()
    trace = store.query_one("SELECT user_hash FROM traces WHERE id = ?", (body.trace_id,))
    if not trace or trace["user_hash"] != hash_user(principal.user_id):
        raise HTTPException(status_code=404, detail="Trace not found")
    tracer.add_score(
        body.trace_id,
        "user_rating",
        1.0 if body.rating > 0 else 0.0,
        source="user",
        comment=(body.comment or None),
    )
    return {"ok": True}


@router.get("/knowledge")
def list_knowledge_documents(_: Principal = Depends(require_admin)):
    """列出 Agent 查询实际检索的共享知识库中的文件。"""
    return {"data": knowledge_library.list_documents()}


@router.post("/knowledge")
async def upload_knowledge_document(file: UploadFile, admin: Principal = Depends(require_admin)):
    """仅限管理员：上传文件到 Agent 的共享知识库。

    用户在 Open WebUI 中的私有知识库相互独立；本接口只操作
    Agent 的 search_knowledge_documents 工具实际查询的集合。
    """
    try:
        return await knowledge_library.upload_document(
            filename=file.filename or "document", content=await file.read(), uploaded_by=admin.email
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"Ingestion service rejected the file: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/knowledge/{file_id}")
async def delete_knowledge_document(file_id: str, _: Principal = Depends(require_admin)):
    """管理员从 Agent 共享知识库删除一个文件；摄取服务拒绝返回 502，服务不可用返回 503。"""
    try:
        return await knowledge_library.delete_document(file_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"Ingestion service rejected the request: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class TenantBody(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)


class GrantBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    tenant_id: str = Field(min_length=1, max_length=128)


@router.get("/tenants")
def list_all_tenants(_: Principal = Depends(require_admin)):
    """返回完整租户目录及每位用户当前的授权，供账户页使用。"""
    from src.midplatform import tenant_access

    return {"tenants": tenant_access.list_tenants(), "grants": tenant_access.list_grants()}


@router.post("/tenants")
def create_tenant(body: TenantBody, _: Principal = Depends(require_admin)):
    """管理员新增一个业务租户；ID 已存在返回 409。"""
    from src.midplatform import tenant_access

    try:
        tenant_access.create_tenant(body.tenant_id, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/tenants/{tenant_id}")
def delete_tenant(tenant_id: str, _: Principal = Depends(require_admin)):
    """管理员删除一个业务租户，并同时清除所有账号对它的授权。"""
    from src.midplatform import tenant_access

    tenant_access.delete_tenant(tenant_id)
    return {"ok": True}


@router.post("/tenants/grant")
def grant_tenant(body: GrantBody, admin: Principal = Depends(require_admin)):
    """管理员授予某邮箱访问指定业务租户的权限，并记录授权人；租户不存在返回 400。"""
    from src.midplatform import tenant_access

    try:
        tenant_access.set_grant(body.email, body.tenant_id, True, admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/tenants/revoke")
def revoke_tenant(body: GrantBody, _: Principal = Depends(require_admin)):
    """管理员撤销某邮箱对指定业务租户的访问权限。"""
    from src.midplatform import tenant_access

    tenant_access.set_grant(body.email, body.tenant_id, False, "")
    return {"ok": True}
