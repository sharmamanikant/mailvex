"""Campaign manager baseline.

Revision ID: 20260824_08
Revises: 20260824_07
"""

revision = "20260824_08"
down_revision = "20260824_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Campaign tables are part of the initial schema; this revision marks the
    # application-level campaign manager contract.
    pass


def downgrade() -> None:
    pass
