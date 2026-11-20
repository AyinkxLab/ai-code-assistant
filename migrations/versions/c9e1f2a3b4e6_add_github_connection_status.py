"""add github connection status

Revision ID: c9e1f2a3b4e6
Revises: b8d0f2a4c6e8
Create Date: 2026-11-19 23:45:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c9e1f2a3b4e6"
down_revision = "b8d0f2a4c6e8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("github_accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "connection_status",
                sa.String(length=20),
                nullable=False,
                server_default="connected",
            )
        )
        batch_op.add_column(
            sa.Column("connection_error_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("github_accounts", schema=None) as batch_op:
        batch_op.drop_column("connection_error_at")
        batch_op.drop_column("connection_status")
