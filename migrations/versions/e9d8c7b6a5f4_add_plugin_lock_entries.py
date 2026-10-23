"""add plugin lock entries

Revision ID: e9d8c7b6a5f4
Revises: a1b2c3d4e5f6, aa11bb22cc33
Create Date: 2026-10-23

Adds the ``plugin_lock_entries`` table from the Phase 9 lockfile model (#394,
migration tracked by #395). This revision also merges the two existing heads so
``flask db upgrade`` reaches a single schema.
"""

import sqlalchemy as sa
from alembic import op

revision = "e9d8c7b6a5f4"
down_revision = ("a1b2c3d4e5f6", "aa11bb22cc33")
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plugin_lock_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=512), nullable=False),
        sa.Column("integrity", sa.String(length=128), nullable=False),
        sa.Column("dependencies", sa.JSON(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "plugin_id", name="uq_plugin_lock_entry"),
    )
    op.create_index(
        op.f("ix_plugin_lock_entries_workspace_id"),
        "plugin_lock_entries",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plugin_lock_entries_owner_id"),
        "plugin_lock_entries",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plugin_lock_entries_plugin_id"),
        "plugin_lock_entries",
        ["plugin_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_plugin_lock_entries_plugin_id"), table_name="plugin_lock_entries")
    op.drop_index(op.f("ix_plugin_lock_entries_owner_id"), table_name="plugin_lock_entries")
    op.drop_index(op.f("ix_plugin_lock_entries_workspace_id"), table_name="plugin_lock_entries")
    op.drop_table("plugin_lock_entries")
