"""Base revision for the --since test."""

import sqlalchemy as sa
from alembic import op

revision = "chain001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("chain_table", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade():
    op.drop_table("chain_table")
