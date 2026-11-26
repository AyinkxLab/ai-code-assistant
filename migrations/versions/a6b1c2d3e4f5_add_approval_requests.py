"""add approval requests

Revision ID: a6b1c2d3e4f5
Revises: b4e2f1a3c5d7
Create Date: 2026-11-26 00:00:00.000000

Phase 11 (#601): one row per sensitive-action approval request. A decision must
come from a second organization administrator; stale pending rows are expired.
"""

import sqlalchemy as sa
from alembic import op

revision = "a6b1c2d3e4f5"
down_revision = "b4e2f1a3c5d7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requester_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["decided_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requester_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "action",
            "target_type",
            "target_id",
            "status",
            name="uq_approval_request_scope_status",
        ),
    )
    with op.batch_alter_table("approval_requests", schema=None) as batch_op:
        batch_op.create_index("ix_approval_requests_workspace_id", ["workspace_id"], unique=False)
        batch_op.create_index("ix_approval_requests_status", ["status"], unique=False)
        batch_op.create_index("ix_approval_requests_requester_id", ["requester_id"], unique=False)
        batch_op.create_index("ix_approval_requests_expires_at", ["expires_at"], unique=False)


def downgrade():
    with op.batch_alter_table("approval_requests", schema=None) as batch_op:
        batch_op.drop_index("ix_approval_requests_expires_at")
        batch_op.drop_index("ix_approval_requests_requester_id")
        batch_op.drop_index("ix_approval_requests_status")
        batch_op.drop_index("ix_approval_requests_workspace_id")
    op.drop_table("approval_requests")
