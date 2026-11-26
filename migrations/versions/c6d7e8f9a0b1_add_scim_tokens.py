"""add SCIM organization bearer tokens

Revision ID: c6d7e8f9a0b1
Revises: c5d6e7f8a9b0
Create Date: 2026-11-25 00:20:00.000000

Phase 11 (#594): organization-scoped SCIM tokens store only a SHA-256 hash of
the secret. There is no data backfill; token rows are created by the admin UI.
"""

import sqlalchemy as sa
from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scim_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revoked_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("scim_tokens", schema=None) as batch_op:
        batch_op.create_index("ix_scim_tokens_owner_id", ["owner_id"], unique=False)
        batch_op.create_index("ix_scim_tokens_workspace_id", ["workspace_id"], unique=False)
        batch_op.create_index("ix_scim_tokens_token_hash", ["token_hash"], unique=True)


def downgrade():
    with op.batch_alter_table("scim_tokens", schema=None) as batch_op:
        batch_op.drop_index("ix_scim_tokens_token_hash")
        batch_op.drop_index("ix_scim_tokens_workspace_id")
        batch_op.drop_index("ix_scim_tokens_owner_id")
    op.drop_table("scim_tokens")
