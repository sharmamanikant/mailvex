"""Phase 10 Sender Account Management: sender OAuth/account columns.

Revision ID: 20260828_21
Revises: 20260828_20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260828_21"
down_revision = "20260828_20"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    cols = _columns("email_accounts")
    adds: list[sa.Column] = []
    if "connection_status" not in cols:
        adds.append(sa.Column("connection_status", sa.String(length=30), server_default="CONNECTED", nullable=False))
    if "oauth_provider_account_id" not in cols:
        adds.append(sa.Column("oauth_provider_account_id", sa.String(length=500), nullable=True))
    if "access_token_encrypted" not in cols:
        adds.append(sa.Column("access_token_encrypted", sa.Text(), nullable=True))
    if "refresh_token_encrypted" not in cols:
        adds.append(sa.Column("refresh_token_encrypted", sa.Text(), nullable=True))
    if "token_expires_at" not in cols:
        adds.append(sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True))
    if "scopes" not in cols:
        adds.append(sa.Column("scopes", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
    if "last_connected_at" not in cols:
        adds.append(sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True))
    if "last_used_at" not in cols:
        adds.append(sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    if "refresh_in_progress" not in cols:
        adds.append(sa.Column("refresh_in_progress", sa.DateTime(timezone=True), nullable=True))
    if "created_by" not in cols:
        adds.append(sa.Column("created_by", sa.Uuid(), nullable=True))
    for column in adds:
        op.add_column("email_accounts", column)


def downgrade() -> None:
    cols = _columns("email_accounts")
    for column in (
        "created_by",
        "refresh_in_progress",
        "last_used_at",
        "last_connected_at",
        "scopes",
        "token_expires_at",
        "refresh_token_encrypted",
        "access_token_encrypted",
        "oauth_provider_account_id",
        "connection_status",
    ):
        if column in cols:
            op.drop_column("email_accounts", column)
