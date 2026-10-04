"""Add SMTP sender configuration fields.

Revision ID: 20260823_05
Revises: 20260823_04
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260823_05"
down_revision = "20260823_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("email_accounts")}
    columns: list[tuple[str, Any]] = [
        ("smtp_host", sa.Column("smtp_host", sa.String(length=255), nullable=True)),
        ("smtp_port", sa.Column("smtp_port", sa.Integer(), nullable=True)),
        ("smtp_tls_mode", sa.Column("smtp_tls_mode", sa.String(length=20), nullable=True)),
        ("smtp_username", sa.Column("smtp_username", sa.String(length=320), nullable=True)),
    ]
    for name, column in columns:
        if name not in existing:
            op.add_column("email_accounts", column)


def downgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("email_accounts")}
    for name in ("smtp_username", "smtp_tls_mode", "smtp_port", "smtp_host"):
        if name in existing:
            op.drop_column("email_accounts", name)
