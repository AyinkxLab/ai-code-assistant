"""add per-conversation generation settings

Revision ID: d5e6f7a8b9c0
Revises: c1a2b3c4d5e6
Create Date: 2026-09-25 15:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d5e6f7a8b9c0"
down_revision = "c1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("model", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("temperature", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("system_prompt", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("system_prompt")
        batch_op.drop_column("temperature")
        batch_op.drop_column("model")
        batch_op.drop_column("provider")
