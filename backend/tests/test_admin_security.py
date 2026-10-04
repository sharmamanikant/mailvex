from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.admin import _require_super_admin
from app.security.permissions import TenantPrincipal


def test_tenant_admin_cannot_grant_super_admin() -> None:
    principal = TenantPrincipal(uuid4(), uuid4(), "admin@example.com", "Admin", ("Admin",))
    with pytest.raises(HTTPException) as error:
        _require_super_admin(principal, "SUPER_ADMIN")
    assert error.value.status_code == 403


def test_super_admin_can_retain_super_admin() -> None:
    principal = TenantPrincipal(uuid4(), uuid4(), "admin@example.com", "Admin", ("Super Admin",))
    _require_super_admin(principal, "SUPER_ADMIN")