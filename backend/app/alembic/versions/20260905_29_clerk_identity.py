"""Phase 10A Clerk identity (System A): link application users to Clerk.

Adds stable ``external_identity_id`` + ``identity_provider`` columns to
``users`` so a verified Clerk session maps to exactly one application
account (Part 3 user mapping/provisioning). Legacy rows keep a NULL
external identity until they are linked.

Revision ID: 20260905_29
Revises: 20260903_28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260905_29"
down_revision = "20260903_28"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspect(op.get_bind()).get_columns(table))


def _has_index(name: str) -> bool:
    return name in {index["name"] for index in inspect(op.get_bind()).get_indexes("users")}


def upgrade() -> None:
    if not _has_column("users", "external_identity_id"):
        op.add_column("users", sa.Column("external_identity_id", sa.String(length=100), nullable=True))
    if not _has_index("ix_users_external_identity_id"):
        op.create_index("ix_users_external_identity_id", "users", ["external_identity_id"], unique=True)
    if not _has_column("users", "identity_provider"):
        op.add_column(
            "users",
            sa.Column("identity_provider", sa.String(length=20), nullable=False, server_default="CLERK"),
        )


def downgrade() -> None:
    if _has_index("ix_users_external_identity_id"):
        op.drop_index("ix_users_external_identity_id", table_name="users")
    if _has_column("users", "identity_provider"):
        op.drop_column("users", "identity_provider")
    if _has_column("users", "external_identity_id"):
        op.drop_column("users", "external_identity_id")