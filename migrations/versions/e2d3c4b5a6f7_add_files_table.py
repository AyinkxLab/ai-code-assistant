"""add persisted uploads (files) table

Revision ID: e2d3c4b5a6f7
Revises: d8e9f0a1b2c3
Create Date: 2026-09-29 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e2d3c4b5a6f7"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_files_workspace_id"), ["workspace_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_files_user_id"), ["user_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_files_conversation_id"), ["conversation_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_files_stored_name"), ["stored_name"], unique=True)


def downgrade():
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_files_stored_name"))
        batch_op.drop_index(batch_op.f("ix_files_conversation_id"))
        batch_op.drop_index(batch_op.f("ix_files_user_id"))
        batch_op.drop_index(batch_op.f("ix_files_workspace_id"))
    op.drop_table("files")
