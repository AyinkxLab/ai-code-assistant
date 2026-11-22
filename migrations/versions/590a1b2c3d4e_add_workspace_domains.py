"""add workspace verified domains

Revision ID: 590a1b2c3d4e
Revises: c9d1e2f3a4b5
Create Date: 2026-11-22

Phase 11 (#590): DNS TXT domain ownership challenges and SSO enforcement.
"""

import sqlalchemy as sa
from alembic import op

revision = "590a1b2c3d4e"
down_revision = "c9d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_domains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("verification_token", sa.String(length=128), nullable=False),
        sa.Column("challenge_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enforce_sso", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain", name="uq_workspace_domains_domain"),
    )
    op.create_index("ix_workspace_domains_workspace_id", "workspace_domains", ["workspace_id"])
    op.create_index("ix_workspace_domains_domain", "workspace_domains", ["domain"])


def downgrade():
    op.drop_index("ix_workspace_domains_domain", table_name="workspace_domains")
    op.drop_index("ix_workspace_domains_workspace_id", table_name="workspace_domains")
    op.drop_table("workspace_domains")
