"""Add the ``rate_limits`` table for persistent per-user rate limiting.

Persists counters across process restarts and shares them between workers
(the in-memory sliding window cannot). One row per bucket key; the daily chat
cap folds the UTC date into the key, so no cleanup job is required.

Revision ID: g1a2b3c4d5e6
Revises: c2d3e4f5a6b7
Create Date: 2026-01-01 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "g1a2b3c4d5e6"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the ``rate_limits`` table and its bucket-key index."""
    op.create_table(
        "rate_limits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rate_limits_key", "rate_limits", ["key"], unique=True)


def downgrade() -> None:
    """Drop the ``rate_limits`` table."""
    op.drop_index("ix_rate_limits_key", table_name="rate_limits")
    op.drop_table("rate_limits")
