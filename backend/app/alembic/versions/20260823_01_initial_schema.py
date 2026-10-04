"""Create the initial tenant-scoped database schema.

Revision ID: 20260823_01
Revises:
"""

from alembic import op

from app.models import (
    Base,
    entities,  # noqa: F401
)

revision = "20260823_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
