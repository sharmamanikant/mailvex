"""Phase 9 #4 migration: sender_pools + sender_pool_members.

Chained to verified head ``20260916_06`` (outbound_messages). Index set is
bit-identical to the model (9 == 9): 4 explicit composite indexes from
``__table_args__`` plus the 5 single-column auto-indexes the model derives from
``index=True`` on ``tenant_id``/``status``/``sender_pool_id``/``sender_id``.

Contract (Phase-9 #28/#4 parity): pools are metadata containers — **no
credentials** (no tokens, refresh tokens, API keys, client secrets, SMTP
passwords). Membership is a pure reference row; tenant isolation is enforced by
every index carrying ``tenant_id`` as a leading column.

Revision ID: 20260916_07
Revises: 20260916_06
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_07"
down_revision: str | None = "20260916_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_indexes() -> None:
    op.create_index(
        "ix_sender_pools_tenant_name", "sender_pools", ["tenant_id", "name"], unique=True
    )
    op.create_index(
        "ix_sender_pools_tenant_status", "sender_pools", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_sender_pool_members_tenant_pool_sender",
        "sender_pool_members",
        ["tenant_id", "sender_pool_id", "sender_id"],
        unique=True,
    )
    op.create_index(
        "ix_sender_pool_members_tenant", "sender_pool_members", ["tenant_id"]
    )
    op.create_index("ix_sender_pools_tenant_id", "sender_pools", ["tenant_id"])
    op.create_index("ix_sender_pools_status", "sender_pools", ["status"])
    op.create_index(
        "ix_sender_pool_members_tenant_id", "sender_pool_members", ["tenant_id"]
    )
    op.create_index(
        "ix_sender_pool_members_sender_pool_id", "sender_pool_members", ["sender_pool_id"]
    )
    op.create_index(
        "ix_sender_pool_members_sender_id", "sender_pool_members", ["sender_id"]
    )


def upgrade() -> None:
    op.create_table(
        "sender_pools",
        sa.Column(
            "id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="ACTIVE", index=True
        ),
        sa.Column(
            "created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True
        ),
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
    )
    op.create_table(
        "sender_pool_members",
        sa.Column(
            "id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "sender_pool_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sender_pools.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "sender_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("senders.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
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
    )
    _create_indexes()


def downgrade() -> None:
    for name in (
        "ix_sender_pool_members_sender_id",
        "ix_sender_pool_members_sender_pool_id",
        "ix_sender_pool_members_tenant_id",
        "ix_sender_pool_members_tenant_pool_sender",
        "ix_sender_pool_members_tenant",
        "ix_sender_pools_status",
        "ix_sender_pools_tenant_id",
        "ix_sender_pools_tenant_status",
        "ix_sender_pools_tenant_name",
    ):
        op.drop_index(name, table_name=(
            "sender_pool_members" if "sender_pool_members" in name else "sender_pools"))
    op.drop_table("sender_pool_members")
    op.drop_table("sender_pools")
