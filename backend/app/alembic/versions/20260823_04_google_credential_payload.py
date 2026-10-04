"""Add encrypted provider credential payload storage.

Revision ID: 20260823_04
Revises: 20260823_03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260823_04"
down_revision = "20260823_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("provider_credentials")}
    if "encrypted_credential_payload" not in columns:
        op.add_column("provider_credentials", sa.Column("encrypted_credential_payload", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("provider_credentials")}
    if "encrypted_credential_payload" in columns:
        op.drop_column("provider_credentials", "encrypted_credential_payload")
