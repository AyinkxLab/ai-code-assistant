"""add notification channel preferences

Revision ID: f3b4c5d6e7f8
Revises: b0c1d2e3f4a5
Create Date: 2026-11-21 00:00:00.000000

Phase 11: per-workspace notification channel preferences (#631).
"""

from alembic import op
import sqlalchemy as sa

revision = "f3b4c5d6e7f8"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_channel_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("email", sa.Boolean(), nullable=False),
        sa.Column("chat", sa.Boolean(), nullable=False),
        sa.Column("webhook", sa.Boolean(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "workspace_id", "event_type", name="uq_notification_channel_workspace_event"
        ),
    )
    op.create_index(
        "ix_notification_channel_preferences_workspace_id",
        "notification_channel_preferences",
        ["workspace_id"],
    )


def downgrade():
    op.drop_index(
        "ix_notification_channel_preferences_workspace_id",
        table_name="notification_channel_preferences",
    )
    op.drop_table("notification_channel_preferences")
