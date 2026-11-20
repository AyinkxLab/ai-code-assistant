"""add curated plugin collections

Revision ID: d2e3f4a5b6c7
Revises: c9e1f2a3b4d5, c9e1f2a3b4e6
Create Date: 2026-11-20 00:00:00.000000

Phase 12: maintainer-curated marketplace collections for onboarding and
security (issue #694). Metadata only; rows never install a plugin or grant a
capability. This revision also merges the two migration branches created by
the background-job and GitHub-connection-status changes.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d2e3f4a5b6c7"
down_revision = ("c9e1f2a3b4d5", "c9e1f2a3b4e6")
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plugin_collections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=280), nullable=False, server_default=""),
        sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("plugin_ids", sa.JSON(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", name="uq_plugin_collections_slug"),
    )
    op.create_index(
        "ix_plugin_collections_featured",
        "plugin_collections",
        ["featured"],
    )


def downgrade():
    op.drop_index("ix_plugin_collections_featured", table_name="plugin_collections")
    op.drop_table("plugin_collections")
