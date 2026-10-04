"""Phase 1 provider-connection foundation.

Creates ``provider_connections``: the tenant-scoped, organization-level
provider authorization (distinct from sender-level ``sender_connections``).
Only Google Workspace OAuth is realized in Phase 1, but the columns/enums are
shaped so Microsoft 365, SendGrid, Zoho and SMTP can be added later without a
structural rewrite.

Credential secrets are never persisted: the row carries only an opaque
encrypted ``credential_reference`` (Fernet) produced by the credential store.

Revision ID: 20260916_01
Revises: 20260911_02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260916_01"
down_revision = "20260911_02"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        import sqlalchemy.dialects.postgresql

        return sqlalchemy.dialects.postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    if _table_exists("provider_connections"):
        return
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("connection_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="CONNECTING"),
        sa.Column("provider_account_id", sa.String(length=500), nullable=True),
        sa.Column("workspace_domain", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("scopes", _json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("credential_reference", sa.Text(), nullable=True),
        sa.Column("credential_version", sa.String(length=50), nullable=False, server_default="v1"),
        sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "connected_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index(
        "ix_provider_connections_tenant_status",
        "provider_connections",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_provider_connections_tenant_provider",
        "provider_connections",
        ["tenant_id", "provider"],
    )
    # Per-tenant uniqueness so two tenants can connect their own Workspace
    # while a single tenant can never create uncontrolled duplicates.
    op.create_index(
        "uq_provider_connections_tenant_provider_account",
        "provider_connections",
        ["tenant_id", "provider", "provider_account_id"],
        unique=True,
        postgresql_where=sa.text("provider_account_id IS NOT NULL"),
        sqlite_where=sa.text("provider_account_id IS NOT NULL"),
    )


def downgrade() -> None:
    if _table_exists("provider_connections"):
        op.drop_index("uq_provider_connections_tenant_provider_account", table_name="provider_connections")
        op.drop_index("ix_provider_connections_tenant_provider", table_name="provider_connections")
        op.drop_index("ix_provider_connections_tenant_status", table_name="provider_connections")
        op.drop_table("provider_connections")