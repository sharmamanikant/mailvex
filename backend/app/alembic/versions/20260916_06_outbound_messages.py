"""Add outbound_messages (Phase 8 #4).

The persistent, provider-neutral send record. ``provider_message_id`` and each
``attempt_count`` transition are written by the send worker; the row name and
this table form the durable idempotency + audit anchor for every Phase 8+
outbound send. Credentials are never stored here.

Revision ID: 20260916_06
Revises: 20260916_05
Create Date: 2026-09-17
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "20260916_06"
down_revision: str | None = "20260916_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "outbound_messages"


def _status_default() -> str:
    return "QUEUED"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sender_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("senders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_connection_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("correlation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_email", sa.String(320), nullable=False),
        sa.Column("reply_to", sa.String(320), nullable=True),
        sa.Column("to_recipients", sa.JSON, nullable=False),
        sa.Column("cc_recipients", sa.JSON, nullable=False),
        sa.Column("bcc_recipients", sa.JSON, nullable=False),
        sa.Column("subject", sa.String(998), nullable=False),
        sa.Column("text_body", sa.Text, nullable=True),
        sa.Column("html_body", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=_status_default()),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_message_id", sa.String(512), nullable=True),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbound_messages_tenant_status_created",
        TABLE,
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_outbound_messages_tenant_correlation",
        TABLE,
        ["tenant_id", "correlation_id"],
        unique=True,
    )
    op.create_index(
        "ix_outbound_messages_tenant_idempotency",
        TABLE,
        ["tenant_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_outbound_messages_tenant_id",
        TABLE,
        ["tenant_id"],
    )
    op.create_index(
        "ix_outbound_messages_sender_id",
        TABLE,
        ["sender_id"],
    )
    op.create_index(
        "ix_outbound_messages_provider_connection_id",
        TABLE,
        ["provider_connection_id"],
    )
    op.create_index(
        "ix_outbound_messages_provider",
        TABLE,
        ["provider"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbound_messages_provider", table_name=TABLE)
    op.drop_index(
        "ix_outbound_messages_provider_connection_id", table_name=TABLE
    )
    op.drop_index("ix_outbound_messages_sender_id", table_name=TABLE)
    op.drop_index("ix_outbound_messages_tenant_id", table_name=TABLE)
    op.drop_index(
        "ix_outbound_messages_tenant_idempotency", table_name=TABLE
    )
    op.drop_index(
        "ix_outbound_messages_tenant_correlation", table_name=TABLE
    )
    op.drop_index(
        "ix_outbound_messages_tenant_status_created", table_name=TABLE
    )
    op.drop_table(TABLE)
