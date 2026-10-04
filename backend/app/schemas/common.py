from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TenantScopedSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    created_at: datetime
    updated_at: datetime


class PageRequest(BaseModel):
    page: int = 1
    page_size: int = 50

    def bounded(self) -> PageRequest:
        return PageRequest(
            page=max(self.page, 1), page_size=min(max(self.page_size, 1), 100)
        )
