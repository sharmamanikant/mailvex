"""Add scheduled message retry and processing metadata.

Revision ID: 20260824_09
Revises: 20260824_08
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260824_09"
down_revision = "20260824_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("scheduled_messages")}
    columns: list[tuple[str, Any]] = [
        ("deferred_until", sa.Column("deferred_until", sa.DateTime(timezone=True), nullable=True)),
        ("failure_reason", sa.Column("failure_reason", sa.Text(), nullable=True)),
        ("max_attempts", sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False)),
        ("processed_at", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True)),
    ]
    for name, column in columns:
        if name not in existing:
            op.add_column("scheduled_messages", column)


def downgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("scheduled_messages")}
    for name in ("processed_at", "max_attempts", "failure_reason", "deferred_until"):
        if name in existing:
            op.drop_column("scheduled_messages", name)
