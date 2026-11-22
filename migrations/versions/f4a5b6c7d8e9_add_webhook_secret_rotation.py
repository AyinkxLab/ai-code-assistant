"""add webhook secret rotation columns

Revision ID: f4a5b6c7d8e9
Revises: e4f5a6b7c8d9
Create Date: 2026-11-22 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = ("e4f5a6b7c8d9", "b6c7d8e9f0a1")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("webhook_subscriptions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("previous_secret", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("webhook_subscriptions", schema=None) as batch_op:
        batch_op.drop_column("previous_secret_expires_at")
        batch_op.drop_column("previous_secret")