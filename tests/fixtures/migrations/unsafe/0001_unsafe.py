"""A migration that uses operations the preflight must flag."""

import sqlalchemy as sa
from alembic import op

revision = "unsafe001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "big_table",
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
    )
    op.create_index("ix_big_table_status", "big_table", ["status"])
    op.alter_column("big_table", "name", type_=sa.String(200))
    op.execute("CREATE INDEX ix_big_table_name ON big_table (name)")


def downgrade():
    op.execute("DROP INDEX ix_big_table_name")
    op.drop_index("ix_big_table_status", table_name="big_table")
    op.drop_column("big_table", "status")
