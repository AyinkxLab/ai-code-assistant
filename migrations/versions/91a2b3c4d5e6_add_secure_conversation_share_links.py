"""Add expiring, token-based conversation links.

Revision ID: 91a2b3c4d5e6
Revises: d8e9f0a1b2c3
"""

import sqlalchemy as sa
from alembic import op

revision = "91a2b3c4d5e6"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversation_shares") as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("token_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "permission",
                sa.String(length=20),
                nullable=False,
                server_default="read_only",
            )
        )
        batch_op.create_index("ix_conversation_shares_token_hash", ["token_hash"], unique=True)
        batch_op.create_index("ix_conversation_shares_expires_at", ["expires_at"], unique=False)


def downgrade():
    op.execute("DELETE FROM conversation_shares WHERE user_id IS NULL")
    with op.batch_alter_table("conversation_shares") as batch_op:
        batch_op.drop_index("ix_conversation_shares_expires_at")
        batch_op.drop_index("ix_conversation_shares_token_hash")
        batch_op.drop_column("permission")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("token_hash")
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
