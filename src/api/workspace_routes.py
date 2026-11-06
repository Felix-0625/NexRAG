"""可见的工作区配置；修改操作仅限管理员。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.ops_routes import require_admin
from src.midplatform import workspace
from src.midplatform.ops_store import dumps, get_ops_store
from src.midplatform.readonly import ReadOnlyRoleRequired
from src.midplatform.tenant_access import Principal, allowed_tenants, get_principal

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("")
def workspace_summary(principal: Principal = Depends(get_principal)):
    """返回当前生效的业务数据连接、导出目录、当前账号可访问的业务范围，以及该账号是否有管理权限。"""
    connection = next(item for item in workspace.public_connections() if item["active"])
    try:
        tenants = allowed_tenants(principal)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        tenants = {}
    return {
        "connection": connection,
        "exports_dir": str(workspace.export_root()),
        "tenants": tenants,
        "can_manage": principal.role == "admin",
    }


class FolderForm(BaseModel):
    path: str = Field(min_length=1, max_length=2048)


@router.post("/exports-folder")
def set_export_folder(body: FolderForm, _: Principal = Depends(require_admin)):
    """管理员绑定导出文件目录；路径须为已存在且可写入的绝对路径，否则返回 400。"""
    try:
        return {"path": workspace.bind_export_root(body.path)}
    except (OSError, ValueError) as exc:
        raise HTTPException(400, "无法绑定目录，请确认绝对路径存在且可以写入") from exc


@router.post("/folders/pick")
def pick_export_folder(_: Principal = Depends(require_admin)):
    """管理员在服务所在的本机弹出文件夹选择框并返回所选路径；选择失败返回 400。"""
    try:
        return {"path": workspace.pick_folder()}
    except (OSError, ValueError, TimeoutError) as exc:
        raise HTTPException(400, "请选择本机文件夹，或直接填写绝对路径") from exc


class DiscoverForm(BaseModel):
    kind: str = Field(pattern="^(sqlite|postgresql)$")
    address: str = Field(min_length=1, max_length=4096)


@router.post("/connections/discover")
def inspect_connection(body: DiscoverForm, _: Principal = Depends(require_admin)):
    """管理员探测一个 SQLite 或 PostgreSQL 数据源并返回其表清单，不保存连接；连接失败或未使用只读角色返回 400。"""
    try:
        return {"tables": workspace.discover(body.kind, body.address)}
    except ReadOnlyRoleRequired as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, "数据库连接失败，请检查类型、路径、网络和凭据") from exc


@router.get("/connections")
def list_connections(_: Principal = Depends(require_admin)):
    """管理员列出已登记的业务数据连接（公开视图）。"""
    return {"data": workspace.public_connections()}


class TableForm(BaseModel):
    name: str = Field(pattern="^[A-Za-z_][A-Za-z0-9_]*$")
    tenant_column: str = Field(pattern="^[A-Za-z_][A-Za-z0-9_]*$")


class ConnectionForm(DiscoverForm):
    name: str = Field(min_length=1, max_length=100)
    tables: list[TableForm] = Field(min_length=1, max_length=30)


@router.post("/connections")
def add_connection(body: ConnectionForm, _: Principal = Depends(require_admin)):
    """管理员登记一个业务数据连接，并选定 1 至 30 张业务表及各自的租户字段；不会自动启用，校验失败返回 400。"""
    try:
        return workspace.save_connection(
            body.name, body.kind, body.address, [item.model_dump() for item in body.tables]
        )
    except ReadOnlyRoleRequired as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, "无法保存连接，请检查业务表和租户字段") from exc


@router.post("/connections/{identifier}/activate")
def activate_connection(identifier: str, _: Principal = Depends(require_admin)):
    """管理员切换生效的业务数据连接（default 为默认数据）；真正切换时会清空全部业务范围授权和租户策略，需重新授权，连接不存在返回 404。"""
    if identifier != "default" and not get_ops_store().query_one(
        "SELECT id FROM business_connections WHERE id=?", (identifier,)
    ):
        raise HTTPException(404, "连接不存在")
    selected = None if identifier == "default" else identifier
    if workspace.get_setting("active_connection") == selected:
        return {"ok": True, "grants_reset": False}
    # 替换后的数据库中租户标签可能重复；不能因为字符串相同，
    # 就把授权或披露审批沿用到不同的数据源。
    generation = workspace.get_setting("business_source_generation", 0) + 1
    from src.midplatform.data_policy import ensure_policy_schema

    ensure_policy_schema()
    with get_ops_store().transaction() as connection:
        connection.execute("DELETE FROM tenant_grants")
        connection.execute("DELETE FROM data_policies WHERE kind='tenant'")
        connection.execute(
            "INSERT OR REPLACE INTO workspace_settings VALUES (?,?)",
            ("active_connection", dumps(selected)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO workspace_settings VALUES (?,?)",
            ("business_source_generation", dumps(generation)),
        )
    return {"ok": True, "grants_reset": True}
