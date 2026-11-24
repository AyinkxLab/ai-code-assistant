"""add legal holds

Revision ID: e9f8a7b6c5d4
Revises: c1d2e3f4a5b6
Create Date: 2026-11-24 00:00:00.000000

Phase 11 (#609): one row per legal hold. ``scope`` is ``audit``, ``workspace``
or ``all``; ``released_at`` being NULL means the hold is active.
"""

import sqlalchemy as sa
from alembic import op

revision = "e9f8a7b6c5d4"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "legal_holds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by_id", sa.Integer(), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["released_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("legal_holds", schema=None) as batch_op:
        batch_op.create_index("ix_legal_holds_workspace_id", ["workspace_id"], unique=False)
        batch_op.create_index(
            "ix_legal_holds_workspace_active", ["workspace_id", "released_at"], unique=False
        )


def downgrade():
    with op.batch_alter_table("legal_holds", schema=None) as batch_op:
        batch_op.drop_index("ix_legal_holds_workspace_active")
        batch_op.drop_index("ix_legal_holds_workspace_id")
    op.drop_table("legal_holds")
