from __future__ import annotations

from uuid import UUID

from fastapi import Depends

from app.security.permissions import TenantPrincipal, get_current_principal

CurrentPrincipal = Depends(get_current_principal)


def get_tenant_id(principal: TenantPrincipal = CurrentPrincipal) -> UUID:
    return principal.tenant_id
