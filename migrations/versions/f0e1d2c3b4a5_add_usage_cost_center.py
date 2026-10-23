"""add usage cost center tag

Revision ID: f0e1d2c3b4a5
Revises: e9d8c7b6a5f4, dd44ee55ff66
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "f0e1d2c3b4a5"
down_revision = ("e9d8c7b6a5f4", "dd44ee55ff66")
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "usage_records",
        sa.Column("cost_center", sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f("ix_usage_records_cost_center"),
        "usage_records",
        ["cost_center"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_usage_records_cost_center"), table_name="usage_records")
    op.drop_column("usage_records", "cost_center")
