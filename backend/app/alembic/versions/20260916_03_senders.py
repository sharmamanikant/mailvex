"""Phase 3 Sender records.

Creates ``senders``: application-level sending identities created from
discovered ``mailboxes``. A Sender always resolves to exactly one Mailbox
within a provider connection and to exactly one tenant. Creation is
idempotent via the unique (tenant_id, mailbox_id) constraint.

Senders start ACTIVE with ``sending_enabled`` False and ``health_status``
UNKNOWN; health fields are populated by the Phase 5 health engine. Credentials
are never stored here - they belong to ``provider_connections``.

Revision ID: 20260916_03
Revises: 20260916_02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260916_03"
down_revision = "20260916_02"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def upgrade() -> None:
    if not _table_exists("mailboxes"):
        raise RuntimeError("mailboxes table missing; run 20260916_02 first")
    if _table_exists("senders"):
        return
    op.create_table(
        "senders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mailbox_id",
            sa.Uuid(),
            sa.ForeignKey("mailboxes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_connection_id",
            sa.Uuid(),
            sa.ForeignKey("provider_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "sending_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "health_status",
            sa.String(length=30),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("health_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index(
        "uq_senders_tenant_mailbox",
        "senders",
        ["tenant_id", "mailbox_id"],
        unique=True,
    )
    op.create_index(
        "ix_senders_tenant_status",
        "senders",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_senders_tenant_connection",
        "senders",
        ["tenant_id", "provider_connection_id"],
    )
    op.create_index(
        "ix_senders_tenant_email",
        "senders",
        ["tenant_id", "email"],
    )
    op.create_index(
        "ix_senders_tenant_provider",
        "senders",
        ["tenant_id", "provider"],
    )


def downgrade() -> None:
    if not _table_exists("senders"):
        return
    op.drop_index("ix_senders_tenant_provider", table_name="senders")
    op.drop_index("ix_senders_tenant_email", table_name="senders")
    op.drop_index("ix_senders_tenant_connection", table_name="senders")
    op.drop_index("ix_senders_tenant_status", table_name="senders")
    op.drop_index("uq_senders_tenant_mailbox", table_name="senders")
    op.drop_table("senders")