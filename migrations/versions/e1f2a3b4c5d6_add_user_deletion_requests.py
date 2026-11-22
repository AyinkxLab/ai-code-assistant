"""add user deletion requests

Revision ID: e1f2a3b4c5d6
Revises: a3b4c5d6e7f8
Create Date: 2026-11-22 00:10:00.000000

Phase 11 (#615): deletion/anonymization request workflow. The account row and
append-only audit rows are never deleted; completion anonymizes the account and
soft-removes memberships.
"""

import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_deletion_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_deletion_requests_user_id",
        "user_deletion_requests",
        ["user_id"],
    )
    op.create_index(
        "ix_user_deletion_requests_status",
        "user_deletion_requests",
        ["status"],
    )
    op.create_index(
        "ix_user_deletion_requests_user_requested",
        "user_deletion_requests",
        ["user_id", "requested_at"],
    )


def downgrade():
    op.drop_index(
        "ix_user_deletion_requests_user_requested",
        table_name="user_deletion_requests",
    )
    op.drop_index(
        "ix_user_deletion_requests_status",
        table_name="user_deletion_requests",
    )
    op.drop_index(
        "ix_user_deletion_requests_user_id",
        table_name="user_deletion_requests",
    )
    op.drop_table("user_deletion_requests")
