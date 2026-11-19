"""add privacy-safe plugin install analytics

Revision ID: a7c9e1b3d5f7
Revises: f0e1d2c3b4a5
"""

import sqlalchemy as sa
from alembic import op

revision = "a7c9e1b3d5f7"
down_revision = "f0e1d2c3b4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plugin_install_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_plugin_install_events_plugin_id",
        "plugin_install_events",
        ["plugin_id"],
    )
    op.create_index(
        "ix_plugin_install_events_action",
        "plugin_install_events",
        ["action"],
    )
    op.create_index(
        "ix_plugin_install_events_created_at",
        "plugin_install_events",
        ["created_at"],
    )


def downgrade():
    op.drop_index("ix_plugin_install_events_created_at", table_name="plugin_install_events")
    op.drop_index("ix_plugin_install_events_action", table_name="plugin_install_events")
    op.drop_index("ix_plugin_install_events_plugin_id", table_name="plugin_install_events")
    op.drop_table("plugin_install_events")
