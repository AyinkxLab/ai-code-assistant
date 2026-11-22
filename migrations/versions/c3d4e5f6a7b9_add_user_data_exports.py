"""add user data export requests

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-11-22 00:00:00.000000

Phase 11 (#614): account-data export requests with an auditable lifecycle and
a one-time, expiring download token (only its SHA-256 hash is stored).
"""

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b9"
down_revision = "b2c3d4e5f6a8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_data_exports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_data_exports_user_id", "user_data_exports", ["user_id"])
    op.create_index("ix_user_data_exports_status", "user_data_exports", ["status"])
    op.create_index(
        "ix_user_data_exports_token_hash",
        "user_data_exports",
        ["token_hash"],
        unique=True,
    )
    op.create_index("ix_user_data_exports_expires_at", "user_data_exports", ["expires_at"])
    op.create_index(
        "ix_user_data_exports_user_requested",
        "user_data_exports",
        ["user_id", "requested_at"],
    )


def downgrade():
    op.drop_index("ix_user_data_exports_user_requested", table_name="user_data_exports")
    op.drop_index("ix_user_data_exports_expires_at", table_name="user_data_exports")
    op.drop_index("ix_user_data_exports_token_hash", table_name="user_data_exports")
    op.drop_index("ix_user_data_exports_status", table_name="user_data_exports")
    op.drop_index("ix_user_data_exports_user_id", table_name="user_data_exports")
    op.drop_table("user_data_exports")
