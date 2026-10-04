"""Harden import jobs for the production recipient-import engine.

Revision ID: 20260828_16
Revises: 20260827_15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260828_16"
down_revision = "20260827_15"
branch_labels = None
depends_on = None

_COLUMNS: list[tuple[str, sa.Column]] = [
    ("duplicate_policy", sa.Column("duplicate_policy", sa.String(length=20), server_default="SKIP", nullable=False)),
    ("preview", sa.Column("preview", sa.JSON(), nullable=True)),
    ("total_rows", sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False)),
    ("processed_rows", sa.Column("processed_rows", sa.Integer(), server_default="0", nullable=False)),
    ("successful_rows", sa.Column("successful_rows", sa.Integer(), server_default="0", nullable=False)),
    ("failed_rows", sa.Column("failed_rows", sa.Integer(), server_default="0", nullable=False)),
    ("duplicate_rows", sa.Column("duplicate_rows", sa.Integer(), server_default="0", nullable=False)),
    ("suppressed_rows", sa.Column("suppressed_rows", sa.Integer(), server_default="0", nullable=False)),
    ("updated_rows", sa.Column("updated_rows", sa.Integer(), server_default="0", nullable=False)),
    ("error_message", sa.Column("error_message", sa.String(length=1000), nullable=True)),
    ("started_at", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)),
    ("finished_at", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)),
]


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("import_jobs")}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("import_jobs", column)
    op.execute("UPDATE import_jobs SET status = 'READY' WHERE status = 'QUEUED'")
    op.execute("UPDATE import_jobs SET status = 'FAILED' WHERE status = 'RUNNING'")


def downgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("import_jobs")}
    for name, _ in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("import_jobs", name)