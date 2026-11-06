"""列出并下载 Agent 导出的分析文件。

可见性与记忆采用同一条硬边界：已登录用户只能看到自己的导出文件
（``Principal.user_id``），与角色无关。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from src.agent.exports import list_exports
from src.midplatform.tenant_access import Principal, get_principal
from src.midplatform.workspace import export_root

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get("")
def list_my_exports(chat_id: str | None = None, principal: Principal = Depends(get_principal)):
    """列出当前登录用户自己的导出文件，可按对话 ID 过滤；看不到他人的文件。"""
    return {"data": list_exports(principal.user_id, chat_id)}


@router.get("/download/{relative_path:path}")
def download_export(relative_path: str, principal: Principal = Depends(get_principal)):
    """下载当前登录用户自己的导出文件；路径越出其个人目录或文件不存在时一律返回 404。"""
    root = export_root()
    target = (root / relative_path).resolve()
    user_root = (root / principal.user_id).resolve()
    if (
        principal.user_id in {".", ".."}
        or not user_root.is_relative_to(root.resolve())
        or not target.is_relative_to(user_root)
    ):
        raise HTTPException(status_code=404, detail="Export not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(target, filename=target.name)
