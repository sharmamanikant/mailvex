"""Add AI workflow and token/cost metadata.

Revision ID: 20260824_07
Revises: 20260823_06
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260824_07"
down_revision = "20260823_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("ai_generations")}
    columns: list[tuple[str, Any]] = [
        ("workflow_status", sa.Column("workflow_status", sa.String(length=30), server_default="DRAFT", nullable=False)),
        ("prompt_tokens", sa.Column("prompt_tokens", sa.Integer(), nullable=True)),
        ("completion_tokens", sa.Column("completion_tokens", sa.Integer(), nullable=True)),
        ("total_tokens", sa.Column("total_tokens", sa.Integer(), nullable=True)),
        ("estimated_cost", sa.Column("estimated_cost", sa.Numeric(precision=18, scale=8), nullable=True)),
        ("cost_currency", sa.Column("cost_currency", sa.String(length=3), server_default="USD", nullable=False)),
    ]
    for name, column in columns:
        if name not in existing:
            op.add_column("ai_generations", column)


def downgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("ai_generations")}
    for name in ("cost_currency", "estimated_cost", "total_tokens", "completion_tokens", "prompt_tokens", "workflow_status"):
        if name in existing:
            op.drop_column("ai_generations", name)
