"""Add refresh and password-reset token storage.

Revision ID: 20260823_02
Revises: 20260823_01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260823_02"
down_revision = "20260823_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("refresh_tokens"):
        op.create_table(
            "refresh_tokens",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["replaced_by_id"], ["refresh_tokens.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
            sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        )
        op.create_index("ix_refresh_tokens_tenant_id", "refresh_tokens", ["tenant_id"])
        op.create_index("ix_refresh_tokens_active", "refresh_tokens", ["tenant_id", "user_id", "expires_at"])
    if not inspector.has_table("password_reset_tokens"):
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name="pk_password_reset_tokens"),
            sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
        )
        op.create_index("ix_password_reset_tokens_tenant_id", "password_reset_tokens", ["tenant_id"])
        op.create_index("ix_password_reset_tokens_active", "password_reset_tokens", ["tenant_id", "user_id", "expires_at"])


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if inspector.has_table("password_reset_tokens"):
        op.drop_table("password_reset_tokens")
    if inspector.has_table("refresh_tokens"):
        op.drop_table("refresh_tokens")
