"""add plugin error reports table

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-08 00:00:00.000000

Adds the bounded ``plugin_error_reports`` table that records safe facts about
plugin failures (plugin id, operation, exception type, truncated message,
optional workspace). No stack traces, payloads, or secrets are stored.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plugin_error_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=160), nullable=False),
        sa.Column("exception_type", sa.String(length=200), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("plugin_error_reports", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_plugin_error_reports_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_plugin_error_reports_plugin_id"), ["plugin_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("plugin_error_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugin_error_reports_plugin_id"))
        batch_op.drop_index(batch_op.f("ix_plugin_error_reports_workspace_id"))

    op.drop_table("plugin_error_reports")
