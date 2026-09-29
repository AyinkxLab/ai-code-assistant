"""Persist private file analysis history.

Revision ID: 91a2b3c4d5e8
Revises: d8e9f0a1b2c3
"""

from alembic import op
import sqlalchemy as sa

revision = "91a2b3c4d5e8"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analyzed_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "filename", "content_hash", name="uq_analyzed_file_owner_name_hash"),
    )
    op.create_index("ix_analyzed_files_user_id", "analyzed_files", ["user_id"])
    op.create_table(
        "file_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("analyzed_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_file_analyses_file_id", "file_analyses", ["file_id"])
    op.create_index("ix_file_analyses_user_id", "file_analyses", ["user_id"])
    op.create_index("ix_file_analyses_created_at", "file_analyses", ["created_at"])


def downgrade():
    op.drop_index("ix_file_analyses_created_at", table_name="file_analyses")
    op.drop_index("ix_file_analyses_user_id", table_name="file_analyses")
    op.drop_index("ix_file_analyses_file_id", table_name="file_analyses")
    op.drop_table("file_analyses")
    op.drop_index("ix_analyzed_files_user_id", table_name="analyzed_files")
    op.drop_table("analyzed_files")
