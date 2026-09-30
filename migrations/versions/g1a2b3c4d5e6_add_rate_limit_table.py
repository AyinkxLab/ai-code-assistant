"""Add rate_limits table for persistent per-user chat rate limiting.

Revision ID: g1a2b3c4d5e6
Revisions: <create new>
Create Date: 2024-01-01 00:00:00.000000

"""

from alembic import op

import sqlalchema as sa
from sqlalchemy import sa

# revision identifiers, used by Alembic.
revision = "g1a2b3c4d5e6"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the rate_limits table."""
    op.create_table(
        "rate_limits",
        sa.Column("id", sa.Integer, nullable=False, primary_key=True),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rate_limits_key", "rate_limits", ["key"], unique=True)


def downgrade() -> None:
    """Drop the rate_limits table."""
    op.drop_index("ix_rate_limits_key", table_name="rate_limits")
    op.drop_table("rate_limits")
