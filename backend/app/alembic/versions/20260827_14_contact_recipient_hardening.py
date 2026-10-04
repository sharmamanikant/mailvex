"""Add contact notes and production query indexes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260827_14"
down_revision = "20260826_13"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if "notes" not in _columns("contacts"):
        op.add_column("contacts", sa.Column("notes", sa.Text(), nullable=True))
    indexes = _indexes("contacts")
    for name, columns in (
        ("ix_contacts_tenant_company", ["tenant_id", "company"]),
        ("ix_contacts_tenant_designation", ["tenant_id", "designation"]),
        ("ix_contacts_tenant_location", ["tenant_id", "location"]),
        ("ix_contacts_tenant_created", ["tenant_id", "created_at"]),
    ):
        if name not in indexes:
            op.create_index(name, "contacts", columns)


def downgrade() -> None:
    indexes = _indexes("contacts")
    for name in (
        "ix_contacts_tenant_created",
        "ix_contacts_tenant_location",
        "ix_contacts_tenant_designation",
        "ix_contacts_tenant_company",
    ):
        if name in indexes:
            op.drop_index(name, table_name="contacts")
    if "notes" in _columns("contacts"):
        op.drop_column("contacts", "notes")
