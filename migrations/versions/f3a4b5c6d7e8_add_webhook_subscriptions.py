"""add webhook secret hashes and event filters

Revision ID: f3a4b5c6d7e8
Revises: f2a3b4c5d6e7, f3b4c5d6e7f8
Create Date: 2026-11-21 12:00:00.000000

Extends the Phase 12 webhook subscription table (#667) with a secret hash, an
active-subscription index, and a normalized event-filter table. Merges the two
pre-existing heads so ``flask db upgrade`` still reaches a single schema.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f3a4b5c6d7e8"
down_revision = ("f2a3b4c5d6e7", "f3b4c5d6e7f8")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("webhook_subscriptions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("secret_hash", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_webhook_subscriptions_active", ["is_active"], unique=False
        )

    op.create_table(
        "webhook_subscription_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["webhook_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id", "event_type", name="uq_webhook_subscription_event"
        ),
    )
    with op.batch_alter_table("webhook_subscription_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_webhook_subscription_events_subscription_id"),
            ["subscription_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_webhook_subscription_events_event_type"),
            ["event_type"],
            unique=False,
        )
        batch_op.create_index(
            "ix_webhook_subscription_events_type_subscription",
            ["event_type", "subscription_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("webhook_subscription_events", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_subscription_events_type_subscription")
        batch_op.drop_index(batch_op.f("ix_webhook_subscription_events_event_type"))
        batch_op.drop_index(batch_op.f("ix_webhook_subscription_events_subscription_id"))

    op.drop_table("webhook_subscription_events")

    with op.batch_alter_table("webhook_subscriptions", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_subscriptions_active")
        batch_op.drop_column("secret_hash")
