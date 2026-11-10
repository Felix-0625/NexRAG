"""List and download agent-generated analysis files.

As with memory, authenticated users see only their own exports by Principal.user_id,
regardless of administrator role.
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
    """List only the caller's exports, optionally filtered by conversation ID."""
    return {"data": list_exports(principal.user_id, chat_id)}


@router.get("/download/{relative_path:path}")
def download_export(relative_path: str, principal: Principal = Depends(get_principal)):
    """Download only the caller's export; return 404 for missing files or paths outside their
    directory.
    """
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
