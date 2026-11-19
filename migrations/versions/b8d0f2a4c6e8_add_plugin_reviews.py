"""add marketplace plugin ratings and reviews

Revision ID: b8d0f2a4c6e8
Revises: a7c9e1b3d5f7
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d0f2a4c6e8"
down_revision = "a7c9e1b3d5f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plugin_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_reason", sa.Text(), nullable=True),
        sa.Column("owner_response", sa.Text(), nullable=True),
        sa.Column("owner_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_response_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugins.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["owner_response_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("plugin_id", "user_id", name="uq_plugin_review_user"),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_plugin_review_rating"),
    )
    op.create_index("ix_plugin_reviews_plugin_id", "plugin_reviews", ["plugin_id"])
    op.create_index("ix_plugin_reviews_user_id", "plugin_reviews", ["user_id"])


def downgrade():
    op.drop_index("ix_plugin_reviews_user_id", table_name="plugin_reviews")
    op.drop_index("ix_plugin_reviews_plugin_id", table_name="plugin_reviews")
    op.drop_table("plugin_reviews")
