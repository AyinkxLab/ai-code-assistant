"""add SIEM forwarding endpoints

Revision ID: e8f9a0b1c2d3
Revises: c3d4e5f6a7b9
Create Date: 2026-11-22 00:00:00.000000

Phase 11 (#629): one signed SIEM webhook endpoint per workspace.
"""

import sqlalchemy as sa
from alembic import op

revision = "e8f9a0b1c2d3"
down_revision = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "siem_endpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("secret", sa.String(length=255), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_siem_endpoints_workspace"),
    )
    op.create_index("ix_siem_endpoints_workspace_id", "siem_endpoints", ["workspace_id"])


def downgrade():
    op.drop_index("ix_siem_endpoints_workspace_id", table_name="siem_endpoints")
    op.drop_table("siem_endpoints")
