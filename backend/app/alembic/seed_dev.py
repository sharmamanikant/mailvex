from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Permission, Role, Tenant

DEV_PERMISSIONS = (
    "contacts.read",
    "contacts.create",
    "contacts.update",
    "contacts.delete",
    "campaigns.read",
    "campaigns.create",
    "campaigns.update",
    "campaigns.approve",
    "campaigns.pause",
    "senders.read",
    "senders.connect",
    "senders.disconnect",
    "integrations.read",
    "integrations.connect",
    "integrations.disconnect",
    "integrations.discover",
    "analytics.read",
    "templates.read",
    "templates.create",
    "templates.update",
    "settings.manage",
    "audit.read",
)


def seed_dev() -> None:
    if os.getenv("APP_ENV", "development") != "development":
        raise RuntimeError("Development seed is disabled outside APP_ENV=development")
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session, session.begin():
        tenant = Tenant(name="Development Tenant", slug="development")
        session.add(tenant)
        permissions = [
            Permission(key=key, description="Development permission")
            for key in DEV_PERMISSIONS
        ]
        session.add_all(permissions)
        session.add(Role(tenant_id=tenant.id, name="Admin", is_system=True))


if __name__ == "__main__":
    seed_dev()
