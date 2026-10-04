from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.base import Base


class TenantScopedRepository[T: Base]:
    """Repository primitive that requires tenant scope for every lookup."""

    def __init__(self, session: Session, model: type[T], tenant_id: UUID) -> None:
        self.session = session
        self.model = model
        self.tenant_id = tenant_id

    def scoped_select(self) -> Select[tuple[T]]:
        return select(self.model).where(self.model.tenant_id == self.tenant_id)  # type: ignore[attr-defined]

    def get(self, entity_id: UUID) -> T | None:
        statement = self.scoped_select().where(self.model.id == entity_id)  # type: ignore[attr-defined]
        return self.session.scalars(statement).one_or_none()

    def add(self, entity: T) -> T:
        if entity.tenant_id != self.tenant_id:  # type: ignore[attr-defined]
            raise ValueError("Entity tenant does not match repository tenant")
        self.session.add(entity)
        return entity
