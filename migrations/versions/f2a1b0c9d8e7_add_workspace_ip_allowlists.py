"""add workspace IP allowlists

Revision ID: f2a1b0c9d8e7
Revises: e1f2a3b4c5d6
Create Date: 2026-11-22 00:20:00.000000

Phase 11 (#620): one allowlist row per workspace. CIDRs are stored as a JSON
array of canonical strings; ``enabled`` defaults to false so upgrades do not
change access until an owner configures it.
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a1b0c9d8e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_ip_allowlists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cidrs", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id"),
    )


def downgrade():
    op.drop_table("workspace_ip_allowlists")
