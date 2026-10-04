"""Add persisted source reference for contact imports.

Revision ID: 20260823_03
Revises: 20260823_02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260823_03"
down_revision = "20260823_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "source_file_ref" not in {column["name"] for column in inspect(op.get_bind()).get_columns("import_jobs")}:
        op.add_column("import_jobs", sa.Column("source_file_ref", sa.String(length=500), nullable=True))


def downgrade() -> None:
    if "source_file_ref" in {column["name"] for column in inspect(op.get_bind()).get_columns("import_jobs")}:
        op.drop_column("import_jobs", "source_file_ref")
