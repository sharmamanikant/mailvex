from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.base import Base
from app.repositories.tenant_scoped import TenantScopedRepository


class TenantAccessService:
    """Application-level guard for tenant ownership before business operations."""

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def repository(self, model: type[Base]) -> TenantScopedRepository[Base]:
        return TenantScopedRepository(self.session, model, self.tenant_id)

    def require_owned(self, entity: Base) -> Base:
        if entity.tenant_id != self.tenant_id:  # type: ignore[attr-defined]
            raise PermissionError("Resource is outside the active tenant")
        return entity
