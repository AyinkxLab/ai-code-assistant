"""add tracked user sessions

Revision ID: a7e1c2d3b4f5
Revises: e9f8a7b6c5d4
Create Date: 2026-11-24 00:10:00.000000

Phase 11 (#619): one row per login session. The opaque token is the primary
key; a non-NULL ``revoked_at`` marks a session that must no longer authenticate.
"""

import sqlalchemy as sa
from alembic import op

revision = "a7e1c2d3b4f5"
down_revision = "e9f8a7b6c5d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.Integer(), nullable=True),
        sa.Column("revoke_reason", sa.String(length=255), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["revoked_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.create_index("ix_user_sessions_user_id", ["user_id"], unique=False)


def downgrade():
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_user_sessions_user_id")
    op.drop_table("user_sessions")
