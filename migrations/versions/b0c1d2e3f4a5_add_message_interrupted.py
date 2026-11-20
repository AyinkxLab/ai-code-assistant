"""add interrupted flag to messages

Revision ID: b0c1d2e3f4a5
Revises: a9b8c7d6e5f4
Create Date: 2026-11-20

Persists partial assistant replies that were interrupted mid-generation, so a
page reload shows them flagged instead of dropping them (#24).
"""

import sqlalchemy as sa
from alembic import op

revision = "b0c1d2e3f4a5"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "messages",
        sa.Column("interrupted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("messages", "interrupted")
