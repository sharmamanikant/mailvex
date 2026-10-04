"""Phase 10A email-provider foundation: sender connections and sender
accounts (System B, independent of Clerk).

A connection carries an encrypted *credential_reference* pointing to the
secure credential store; plaintext secrets are never persisted on the row.

Revision ID: 20260903_28
Revises: 20260902_27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260903_28"
down_revision = "20260902_27"
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
    if not _table_exists("sender_connections"):
        op.create_table(
            "sender_connections",
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
            sa.Column("external_account_id", sa.String(length=500), nullable=True),
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column("credential_reference", sa.Text(), nullable=True),
            sa.Column("credential_version", sa.String(length=50), nullable=False, server_default="v1"),
            sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_by",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index(
            "ix_sender_connections_tenant_status",
            "sender_connections",
            ["tenant_id", "status"],
        )

    if not _table_exists("sender_accounts"):
        op.create_table(
            "sender_accounts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "connection_id",
                sa.Uuid(),
                sa.ForeignKey("sender_connections.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=True),
            sa.Column("provider", sa.String(length=30), nullable=False),
            sa.Column("external_sender_id", sa.String(length=500), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
            sa.Column("health_status", sa.String(length=30), nullable=False, server_default="UNKNOWN"),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("tenant_id", "connection_id", "email", name="uq_sender_accounts_tenant_connection_email"),
        )
        op.create_index(
            "ix_sender_accounts_tenant_connection",
            "sender_accounts",
            ["tenant_id", "connection_id"],
        )
        op.create_index(
            "ix_sender_accounts_tenant_status",
            "sender_accounts",
            ["tenant_id", "status"],
        )


def downgrade() -> None:
    if _table_exists("sender_accounts"):
        op.drop_index("ix_sender_accounts_tenant_status", table_name="sender_accounts")
        op.drop_index("ix_sender_accounts_tenant_connection", table_name="sender_accounts")
        op.drop_table("sender_accounts")
    if _table_exists("sender_connections"):
        op.drop_index("ix_sender_connections_tenant_status", table_name="sender_connections")
        op.drop_table("sender_connections")