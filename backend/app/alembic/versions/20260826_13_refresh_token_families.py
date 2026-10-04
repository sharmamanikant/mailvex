"""Track refresh-token families for replay containment."""

import sqlalchemy as sa
from alembic import op

revision = "20260826_13"
down_revision = "20260825_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("family_id", sa.Uuid(), nullable=True))
    # Existing tokens become the root of their own family.  This is portable
    # across supported PostgreSQL versions and does not require pgcrypto.
    op.execute("UPDATE refresh_tokens SET family_id = id WHERE family_id IS NULL")
    op.alter_column("refresh_tokens", "family_id", nullable=False)
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_foreign_key(
        "fk_refresh_tokens_family_id_refresh_tokens",
        "refresh_tokens",
        "refresh_tokens",
        ["family_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_refresh_tokens_family_id_refresh_tokens", "refresh_tokens", type_="foreignkey")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "family_id")
