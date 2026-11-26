"""add SCIM group role mappings

Revision ID: b7c2d3e4f5a6
Revises: a6b1c2d3e4f5
Create Date: 2026-11-26 00:00:00.000000

Phase 11 (#593): store minimal SCIM group records and safe group-to-role
mappings. ``workspace_members.provisioning_source`` lets SCIM revoke only the
memberships it created; existing rows default to ``manual``.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c2d3e4f5a6"
down_revision = "a6b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "group_role_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("group_external_id", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "group_external_id",
            name="uq_group_role_mapping_workspace_group",
        ),
    )
    with op.batch_alter_table("group_role_mappings", schema=None) as batch_op:
        batch_op.create_index("ix_group_role_mappings_workspace_id", ["workspace_id"], unique=False)

    op.create_table(
        "scim_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "external_id", name="uq_scim_group_workspace_external"),
    )
    with op.batch_alter_table("scim_groups", schema=None) as batch_op:
        batch_op.create_index("ix_scim_groups_workspace_id", ["workspace_id"], unique=False)

    op.create_table(
        "scim_group_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["scim_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_scim_group_member_group_user"),
    )
    with op.batch_alter_table("scim_group_members", schema=None) as batch_op:
        batch_op.create_index("ix_scim_group_members_group_id", ["group_id"], unique=False)
        batch_op.create_index("ix_scim_group_members_user_id", ["user_id"], unique=False)

    with op.batch_alter_table("workspace_members", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "provisioning_source",
                sa.String(length=16),
                nullable=False,
                server_default="manual",
            )
        )


def downgrade():
    with op.batch_alter_table("workspace_members", schema=None) as batch_op:
        batch_op.drop_column("provisioning_source")
    with op.batch_alter_table("scim_group_members", schema=None) as batch_op:
        batch_op.drop_index("ix_scim_group_members_user_id")
        batch_op.drop_index("ix_scim_group_members_group_id")
    op.drop_table("scim_group_members")
    with op.batch_alter_table("scim_groups", schema=None) as batch_op:
        batch_op.drop_index("ix_scim_groups_workspace_id")
    op.drop_table("scim_groups")
    with op.batch_alter_table("group_role_mappings", schema=None) as batch_op:
        batch_op.drop_index("ix_group_role_mappings_workspace_id")
    op.drop_table("group_role_mappings")
