"""Identity and authorization dependencies for the core API."""

from fastapi import Depends, HTTPException

from src.midplatform.tenant_access import Principal, get_principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    """Require an authenticated administrator, returning 403 otherwise."""
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return principal
