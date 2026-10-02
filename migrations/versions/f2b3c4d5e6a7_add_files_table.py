"""add uploaded files table (issue #42)

Revision ID: f2b3c4d5e6a7
Revises: 91a2b3c4d5e6, 91a2b3c4d5e7, 91a2b3c4d5e8
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b3c4d5e6a7"
# Merge the three pre-existing heads while adding the new table, so the tree
# has a single head again.
down_revision: str | Sequence[str] | None = (
    "91a2b3c4d5e6",
    "91a2b3c4d5e7",
    "91a2b3c4d5e8",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "content_type",
            sa.String(length=100),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_name"),
    )
    op.create_index("ix_files_user_id", "files", ["user_id"])
    op.create_index("ix_files_workspace_id", "files", ["workspace_id"])
    op.create_index("ix_files_conversation_id", "files", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_files_conversation_id", table_name="files")
    op.drop_index("ix_files_workspace_id", table_name="files")
    op.drop_index("ix_files_user_id", table_name="files")
    op.drop_table("files")
