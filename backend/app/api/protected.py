from __future__ import annotations

from fastapi import APIRouter, Depends

from app.security.permissions import TenantPrincipal, require_permission

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(principal: TenantPrincipal = Depends(require_permission("analytics.read"))) -> dict[str, object]:
    return {"tenant_id": str(principal.tenant_id), "user": principal.display_name, "message": "Dashboard ready"}
