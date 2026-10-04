"""Provider catalog alias (Part 12 spec surface).

Exposes ``GET /api/v1/providers`` — a first-class, stable route for the
provider capability catalog, mirroring ``/api/v1/integrations/providers``.
Kept as a thin delegate so System B stays single-sourced in
``IntegrationService``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.integrations import ProviderCapabilityResponse
from app.security.permissions import TenantPrincipal, require_permission
from app.services.integrations import IntegrationService

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=list[ProviderCapabilityResponse])
def list_providers(
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
) -> list[ProviderCapabilityResponse]:
    return [ProviderCapabilityResponse(**cap.__dict__) for cap in IntegrationService(session, principal.tenant_id).list_providers()]