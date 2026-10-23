"""add billing and usage alert notification preferences

The ``budget_alerts`` switch was added to the model with the Phase 10 budget
work but never had a migration, and billing alerts need their own category. Add
both columns, tolerating databases where ``budget_alerts`` already exists (for
example a dev database created from the models).

Revision ID: aa11bb22cc33
Revises: c7d8e9f0a1b2
Create Date: 2026-10-21 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# Unique revision id: the original b7c8d9e0f1a2 duplicated the budgets migration.
revision = "aa11bb22cc33"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("notification_preferences")}


def upgrade():
    columns = _columns()
    with op.batch_alter_table("notification_preferences", schema=None) as batch_op:
        if "budget_alerts" not in columns:
            batch_op.add_column(
                sa.Column("budget_alerts", sa.Boolean(), nullable=False, server_default=sa.true())
            )
        if "billing_alerts" not in columns:
            batch_op.add_column(
                sa.Column("billing_alerts", sa.Boolean(), nullable=False, server_default=sa.true())
            )


def downgrade():
    columns = _columns()
    with op.batch_alter_table("notification_preferences", schema=None) as batch_op:
        if "billing_alerts" in columns:
            batch_op.drop_column("billing_alerts")
        if "budget_alerts" in columns:
            batch_op.drop_column("budget_alerts")
