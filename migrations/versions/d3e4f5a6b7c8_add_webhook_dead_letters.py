"""add webhook dead letters

Revision ID: d3e4f5a6b7c8
Revises: f2a3b4c5d6e7
Create Date: 2026-11-21 00:00:00.000000

Phase 12: retain permanently failed deliveries for owner-driven replay (#671).
"""

from alembic import op
import sqlalchemy as sa

revision = "d3e4f5a6b7c8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "webhook_dead_letters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replayed_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["webhook_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["replayed_by_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_webhook_dead_letters_subscription_id",
        "webhook_dead_letters",
        ["subscription_id"],
    )
    op.create_index(
        "ix_webhook_dead_letters_expires_at",
        "webhook_dead_letters",
        ["expires_at"],
    )
    op.create_index(
        "ix_webhook_dead_letters_subscription_created",
        "webhook_dead_letters",
        ["subscription_id", "created_at"],
    )


def downgrade():
    op.drop_index(
        "ix_webhook_dead_letters_subscription_created",
        table_name="webhook_dead_letters",
    )
    op.drop_index(
        "ix_webhook_dead_letters_expires_at", table_name="webhook_dead_letters"
    )
    op.drop_index(
        "ix_webhook_dead_letters_subscription_id", table_name="webhook_dead_letters"
    )
    op.drop_table("webhook_dead_letters")

