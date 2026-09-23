"""add conversation shares and shares notification preference

Revision ID: a54b1c2d3e4f
Revises: b2c3d4e5f6a7
Create Date: 2026-09-23 17:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a54b1c2d3e4f"
down_revision = "f7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversation_shares",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("shared_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shared_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "user_id", name="uq_conversation_share_user"),
    )
    with op.batch_alter_table("conversation_shares", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_conversation_shares_conversation_id"),
            ["conversation_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_conversation_shares_user_id"), ["user_id"], unique=False
        )

    with op.batch_alter_table("notification_preferences", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("shares", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade():
    with op.batch_alter_table("notification_preferences", schema=None) as batch_op:
        batch_op.drop_column("shares")

    with op.batch_alter_table("conversation_shares", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversation_shares_user_id"))
        batch_op.drop_index(batch_op.f("ix_conversation_shares_conversation_id"))
    op.drop_table("conversation_shares")
