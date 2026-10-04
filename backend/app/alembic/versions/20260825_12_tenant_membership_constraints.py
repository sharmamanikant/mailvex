"""Enforce same-tenant user and team memberships."""

from alembic import op

revision = "20260825_12"
down_revision = "20260824_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_users_tenant_id", "users", ["tenant_id", "id"])
    op.create_unique_constraint("uq_teams_tenant_id", "teams", ["tenant_id", "id"])
    op.drop_constraint("fk_team_members_team_id_teams", "team_members", type_="foreignkey")
    op.drop_constraint("fk_team_members_user_id_users", "team_members", type_="foreignkey")
    op.create_foreign_key("fk_team_members_tenant_team", "team_members", "teams", ["tenant_id", "team_id"], ["tenant_id", "id"], ondelete="CASCADE")
    op.create_foreign_key("fk_team_members_tenant_user", "team_members", "users", ["tenant_id", "user_id"], ["tenant_id", "id"], ondelete="CASCADE")


def downgrade() -> None:
    op.drop_constraint("fk_team_members_tenant_user", "team_members", type_="foreignkey")
    op.drop_constraint("fk_team_members_tenant_team", "team_members", type_="foreignkey")
    op.create_foreign_key("fk_team_members_user_id_users", "team_members", "users", ["user_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_team_members_team_id_teams", "team_members", "teams", ["team_id"], ["id"], ondelete="CASCADE")
    op.drop_constraint("uq_teams_tenant_id", "teams", type_="unique")
    op.drop_constraint("uq_users_tenant_id", "users", type_="unique")