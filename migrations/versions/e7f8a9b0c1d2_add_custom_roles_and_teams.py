"""add custom roles and teams

Revision ID: e7f8a9b0c1d2
Revises: a6b7c8d9e0f1
Create Date: 2026-11-26 00:00:00.000000

Phase 11 (#599): additive team grouping and custom role assignments at
organization, team, and workspace scope. All columns are nullable/defaulted so
existing installations keep their built-in roles until an assignment is made.
"""

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_teams_workspace_name"),
    )
    op.create_index("ix_teams_workspace_id", "teams", ["workspace_id"], unique=False)
    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),
    )
    op.create_index("ix_team_members_team_id", "team_members", ["team_id"], unique=False)
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"], unique=False)
    op.create_table(
        "custom_roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_role", sa.String(length=20), nullable=False),
        sa.Column("granted_capabilities", sa.JSON(), nullable=False),
        sa.Column("denied_capabilities", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "scope_id", "name", name="uq_custom_roles_scope_name"),
    )
    op.create_index("ix_custom_roles_scope", "custom_roles", ["scope"], unique=False)
    op.create_index("ix_custom_roles_scope_id", "custom_roles", ["scope_id"], unique=False)
    op.create_table(
        "role_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("assigned_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["role_id"], ["custom_roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope", "scope_id", "user_id", name="uq_role_assignment_scope_target_user"
        ),
    )
    op.create_index("ix_role_assignments_role_id", "role_assignments", ["role_id"], unique=False)
    op.create_index("ix_role_assignments_user_id", "role_assignments", ["user_id"], unique=False)
    op.create_index("ix_role_assignments_scope", "role_assignments", ["scope"], unique=False)
    op.create_index("ix_role_assignments_scope_id", "role_assignments", ["scope_id"], unique=False)


def downgrade():
    op.drop_index("ix_role_assignments_scope_id", table_name="role_assignments")
    op.drop_index("ix_role_assignments_scope", table_name="role_assignments")
    op.drop_index("ix_role_assignments_user_id", table_name="role_assignments")
    op.drop_index("ix_role_assignments_role_id", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_index("ix_custom_roles_scope_id", table_name="custom_roles")
    op.drop_index("ix_custom_roles_scope", table_name="custom_roles")
    op.drop_table("custom_roles")
    op.drop_index("ix_team_members_user_id", table_name="team_members")
    op.drop_index("ix_team_members_team_id", table_name="team_members")
    op.drop_table("team_members")
    op.drop_index("ix_teams_workspace_id", table_name="teams")
    op.drop_table("teams")
