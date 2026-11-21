"""add gitlab accounts

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-11-21 00:00:00.000000

Phase 12: GitLab OAuth account metadata (#677). The access token itself is
stored encrypted in ``integration_credentials``.
"""

from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gitlab_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("gitlab_user_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("scopes", sa.String(length=500), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id", "gitlab_user_id", name="uq_gitlab_accounts_workspace_user"
        ),
    )
    op.create_index("ix_gitlab_accounts_workspace_id", "gitlab_accounts", ["workspace_id"])


def downgrade():
    op.drop_index("ix_gitlab_accounts_workspace_id", table_name="gitlab_accounts")
    op.drop_table("gitlab_accounts")

