"""add webhook subscriptions and delivery logs

Revision ID: f2a3b4c5d6e7
Revises: b0c1d2e3f4a5
Create Date: 2026-11-20 00:00:00.000000

Phase 12: durable webhook subscriptions and the per-attempt delivery log used by
the delivery-log UI (#670, #672). Delivery rows store status, response code,
latency, and a short error label, never a response body or signing secret.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f2a3b4c5d6e7"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("secret", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_webhook_subscriptions_workspace_id",
        "webhook_subscriptions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_webhook_subscriptions_workspace_created",
        "webhook_subscriptions",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscriptions.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_id",
        "webhook_deliveries",
        ["subscription_id"],
    )
    op.create_index(
        "ix_webhook_deliveries_status",
        "webhook_deliveries",
        ["status"],
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_created",
        "webhook_deliveries",
        ["subscription_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_status_created",
        "webhook_deliveries",
        ["subscription_id", "status", "created_at"],
    )


def downgrade():
    op.drop_index(
        "ix_webhook_deliveries_subscription_status_created",
        table_name="webhook_deliveries",
    )
    op.drop_index(
        "ix_webhook_deliveries_subscription_created",
        table_name="webhook_deliveries",
    )
    op.drop_index("ix_webhook_deliveries_status", table_name="webhook_deliveries")
    op.drop_index(
        "ix_webhook_deliveries_subscription_id",
        table_name="webhook_deliveries",
    )
    op.drop_table("webhook_deliveries")

    op.drop_index(
        "ix_webhook_subscriptions_workspace_created",
        table_name="webhook_subscriptions",
    )
    op.drop_index(
        "ix_webhook_subscriptions_workspace_id",
        table_name="webhook_subscriptions",
    )
    op.drop_table("webhook_subscriptions")
