"""Pending revision with a non-concurrent index."""

from alembic import op

revision = "chain002"
down_revision = "chain001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_chain_id", "chain_table", ["id"])


def downgrade():
    op.drop_index("ix_chain_id", table_name="chain_table")
