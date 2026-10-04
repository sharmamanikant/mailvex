from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import Contact
from app.repositories.tenant_scoped import TenantScopedRepository
from app.services.tenant_access import TenantAccessService


class RecordingSession:
    def add(self, entity: object) -> None:
        self.entity = entity


def test_repository_rejects_entity_from_another_tenant() -> None:
    tenant_id = uuid4()
    repository = TenantScopedRepository(cast(Session, RecordingSession()), Contact, tenant_id)
    contact = Contact(tenant_id=uuid4(), email="recipient@example.com")

    with pytest.raises(ValueError, match="tenant"):
        repository.add(contact)


def test_service_rejects_entity_from_another_tenant() -> None:
    service = TenantAccessService(cast(Session, RecordingSession()), uuid4())
    contact = Contact(tenant_id=uuid4(), email="recipient@example.com")

    with pytest.raises(PermissionError, match="tenant"):
        service.require_owned(contact)
