"""A migration that only uses safe, bounded operations."""

import sqlalchemy as sa
from alembic import op

revision = "safe0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("safe_table", sa.Column("id", sa.Integer(), primary_key=True))
    op.add_column("safe_table", sa.Column("note", sa.String(100), nullable=True))
    op.create_index(
        "ix_safe_table_note",
        "safe_table",
        ["note"],
        postgresql_concurrently=True,
    )


def downgrade():
    op.drop_index("ix_safe_table_note", table_name="safe_table")
    op.drop_column("safe_table", "note")
    op.drop_table("safe_table")
