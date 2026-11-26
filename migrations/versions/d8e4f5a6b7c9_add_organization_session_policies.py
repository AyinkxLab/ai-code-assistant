"""add organization session policies

Revision ID: d8e4f5a6b7c9
Revises: b7c2d3e4f5a6
Create Date: 2026-11-26 00:00:00.000000

Phase 11 (#591): opt-in maximum session age, idle timeout, and sensitive-action
re-authentication rules. A workspace with no policy row keeps the previous
behaviour, so this migration is additive.
"""

import sqlalchemy as sa
from alembic import op

revision = "d8e4f5a6b7c9"
down_revision = "b7c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "organization_session_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("max_session_age_minutes", sa.Integer(), nullable=True),
        sa.Column("idle_timeout_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "require_reauth_for_sensitive_actions",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "reauth_window_minutes",
            sa.Integer(),
            nullable=False,
            server_default="15",
        ),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id"),
    )
    with op.batch_alter_table("organization_session_policies", schema=None) as batch_op:
        batch_op.create_index(
            "ix_organization_session_policies_workspace_id", ["workspace_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("organization_session_policies", schema=None) as batch_op:
        batch_op.drop_index("ix_organization_session_policies_workspace_id")
    op.drop_table("organization_session_policies")
