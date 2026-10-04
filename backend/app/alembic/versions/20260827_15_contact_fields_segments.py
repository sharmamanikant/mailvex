"""Add tenant-defined contact fields and dynamic recipient segments."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260827_15"
down_revision = "20260827_14"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return {table for table in inspect(op.get_bind()).get_table_names()}


def upgrade() -> None:
    tables = _table_names()
    if "contact_field_definitions" not in tables:
        op.create_table(
            "contact_field_definitions",
            sa.Column("key", sa.String(100), nullable=False),
            sa.Column("label", sa.String(150), nullable=False),
            sa.Column("field_type", sa.String(30), nullable=False),
            sa.Column("options", sa.JSON(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tenant_id", "key", name="uq_contact_field_definition_key"),
        )
    if "contact_segments" not in tables:
        op.create_table(
            "contact_segments",
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("description", sa.String(500)),
            sa.Column("filters", sa.JSON(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tenant_id", "name", name="uq_contact_segments_tenant_name"),
        )


def downgrade() -> None:
    tables = _table_names()
    if "contact_segments" in tables:
        op.drop_table("contact_segments")
    if "contact_field_definitions" in tables:
        op.drop_table("contact_field_definitions")
