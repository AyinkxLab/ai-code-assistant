"""add organization plugin governance

Revision ID: b4e2f1a3c5d7
Revises: a7e1c2d3b4f5
Create Date: 2026-11-24 00:20:00.000000

Phase 11 (#634): one install policy per organization (workspace owner) plus
explicit per-plugin approvals for organizations that require them.
"""

import sqlalchemy as sa
from alembic import op

revision = "b4e2f1a3c5d7"
down_revision = "a7e1c2d3b4f5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "organization_plugin_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id"),
    )
    op.create_table(
        "plugin_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "plugin_id", name="uq_plugin_approval_owner_plugin"),
    )
    with op.batch_alter_table("plugin_approvals", schema=None) as batch_op:
        batch_op.create_index("ix_plugin_approvals_owner_id", ["owner_id"], unique=False)


def downgrade():
    with op.batch_alter_table("plugin_approvals", schema=None) as batch_op:
        batch_op.drop_index("ix_plugin_approvals_owner_id")
    op.drop_table("plugin_approvals")
    op.drop_table("organization_plugin_policies")
