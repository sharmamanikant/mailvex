"""Phase 2 workspace mailbox discovery.

Creates ``mailboxes``: tenant-scoped mailbox records discovered from a
provider workspace (Google Workspace Directory in Phase 2). Each mailbox is
owned by a ``provider_connections`` row and holds only normalized profile
data - never credentials. Duplicates are prevented per tenant/connection via
a unique (tenant_id, provider_connection_id, provider_mailbox_id) index.

Also extends ``provider_connections`` with sync-status metadata
(last_sync_status / last_sync_error / last_sync_started_at /
last_sync_completed_at) so the UI can show "Syncing... / Last synced: ...".

Revision ID: 20260916_02
Revises: 20260916_01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260916_02"
down_revision = "20260916_01"
branch_labels = None
depends_on = None

MAILBOX_SYNC_COLUMNS = {
    "last_sync_status": sa.Column("last_sync_status", sa.String(length=30), nullable=True),
    "last_sync_error": sa.Column("last_sync_error", sa.Text(), nullable=True),
    "last_sync_started_at": sa.Column("last_sync_started_at", sa.DateTime(timezone=True), nullable=True),
    "last_sync_completed_at": sa.Column("last_sync_completed_at", sa.DateTime(timezone=True), nullable=True),
}


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def _column_exists(table: str, column: str) -> bool:
    return bool(column in {col["name"] for col in inspect(op.get_bind()).get_columns(table)})


def upgrade() -> None:
    if not _table_exists("provider_connections"):
        raise RuntimeError("provider_connections table missing; run 20260916_01 first")

    # Sync-status metadata on provider_connections (idempotent per column).
    for column_name, column in MAILBOX_SYNC_COLUMNS.items():
        if not _column_exists("provider_connections", column_name):
            op.add_column("provider_connections", column)

    if _table_exists("mailboxes"):
        return
    op.create_table(
        "mailboxes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_connection_id",
            sa.Uuid(),
            sa.ForeignKey("provider_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_mailbox_id", sa.String(length=500), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("first_name", sa.String(length=200), nullable=True),
        sa.Column("last_name", sa.String(length=200), nullable=True),
        sa.Column("department", sa.String(length=200), nullable=True),
        sa.Column("job_title", sa.String(length=255), nullable=True),
        sa.Column("user_type", sa.String(length=30), nullable=False, server_default="USER"),
        sa.Column("provider_status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column("is_suspended", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index(
        "uq_mailboxes_tenant_connection_mailbox",
        "mailboxes",
        ["tenant_id", "provider_connection_id", "provider_mailbox_id"],
        unique=True,
    )
    op.create_index(
        "ix_mailboxes_tenant_connection",
        "mailboxes",
        ["tenant_id", "provider_connection_id"],
    )
    op.create_index(
        "ix_mailboxes_tenant_email",
        "mailboxes",
        ["tenant_id", "email"],
    )


def downgrade() -> None:
    if _table_exists("mailboxes"):
        op.drop_index("ix_mailboxes_tenant_email", table_name="mailboxes")
        op.drop_index("ix_mailboxes_tenant_connection", table_name="mailboxes")
        op.drop_index("uq_mailboxes_tenant_connection_mailbox", table_name="mailboxes")
        op.drop_table("mailboxes")
    for column_name in reversed(tuple(MAILBOX_SYNC_COLUMNS)):
        if _column_exists("provider_connections", column_name):
            op.drop_column("provider_connections", column_name)