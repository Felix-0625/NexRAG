"""核心 API 的身份与权限依赖。"""

from fastapi import Depends, HTTPException

from src.midplatform.tenant_access import Principal, get_principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    """依赖项：要求已验证身份的角色为 admin，否则返回 403；通过则返回该身份。"""
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return principal
