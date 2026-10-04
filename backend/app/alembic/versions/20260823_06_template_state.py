"""Add template lifecycle and current version pointers.

Revision ID: 20260823_06
Revises: 20260823_05
"""


import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260823_06"
down_revision = "20260823_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("templates")}
    if "status" not in existing:
        op.add_column("templates", sa.Column("status", sa.String(length=30), server_default="DRAFT", nullable=False))
    if "current_version_id" not in existing:
        op.add_column("templates", sa.Column("current_version_id", sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_templates_current_version_id_template_versions", "templates", "template_versions", ["current_version_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("templates")}
    if "current_version_id" in existing:
        with op.batch_alter_table("templates") as batch:
            batch.drop_column("current_version_id")
    if "status" in existing:
        op.drop_column("templates", "status")