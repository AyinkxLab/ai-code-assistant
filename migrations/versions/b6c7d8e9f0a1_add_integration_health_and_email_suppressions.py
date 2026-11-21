"""Add integration health and email suppression tables (#678-#684).

Revision ID: b6c7d8e9f0a1
Revises: e4f5a6b7c8d9
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: str | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_health",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("integration", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="unknown", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("last_error_code", sa.String(length=50), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "integration",
            name="uq_integration_health_workspace_integration",
        ),
    )
    op.create_index(
        "ix_integration_health_workspace_id",
        "integration_health",
        ["workspace_id"],
    )
    op.create_index(
        "ix_integration_health_integration",
        "integration_health",
        ["integration"],
    )

    op.create_table(
        "email_suppressions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column(
            "reason", sa.String(length=50), server_default="unsubscribe", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "email",
            name="uq_email_suppressions_workspace_email",
        ),
    )
    op.create_index(
        "ix_email_suppressions_workspace_id",
        "email_suppressions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_email_suppressions_email",
        "email_suppressions",
        ["email"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_suppressions_email", table_name="email_suppressions")
    op.drop_index("ix_email_suppressions_workspace_id", table_name="email_suppressions")
    op.drop_table("email_suppressions")
    op.drop_index("ix_integration_health_integration", table_name="integration_health")
    op.drop_index("ix_integration_health_workspace_id", table_name="integration_health")
    op.drop_table("integration_health")
