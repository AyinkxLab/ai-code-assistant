"""add security ticket links

Revision ID: a3b4c5d6e7f8
Revises: 590a1b2c3d4e
Create Date: 2026-11-22 00:00:00.000000

Phase 11 (#630): durable Jira/Linear ticket links for high-risk findings. The
unique ``(workspace_id, finding_id)`` row is the idempotency guard; credentials
remain encrypted in ``integration_credentials``.
"""

import sqlalchemy as sa
from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "590a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "security_ticket_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("finding_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("external_key", sa.String(length=100), nullable=False),
        sa.Column("external_url", sa.String(length=500), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["finding_id"], ["review_findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "finding_id",
            name="uq_security_ticket_links_workspace_finding",
        ),
        sa.UniqueConstraint(
            "provider",
            "external_key",
            name="uq_security_ticket_links_provider_external",
        ),
    )
    op.create_index(
        "ix_security_ticket_links_workspace_id",
        "security_ticket_links",
        ["workspace_id"],
    )
    op.create_index(
        "ix_security_ticket_links_finding_id",
        "security_ticket_links",
        ["finding_id"],
    )
    op.create_index(
        "ix_security_ticket_links_external_key",
        "security_ticket_links",
        ["external_key"],
    )
    op.create_index(
        "ix_security_ticket_links_workspace_created",
        "security_ticket_links",
        ["workspace_id", "created_at"],
    )


def downgrade():
    op.drop_index(
        "ix_security_ticket_links_workspace_created",
        table_name="security_ticket_links",
    )
    op.drop_index(
        "ix_security_ticket_links_external_key",
        table_name="security_ticket_links",
    )
    op.drop_index(
        "ix_security_ticket_links_finding_id",
        table_name="security_ticket_links",
    )
    op.drop_index(
        "ix_security_ticket_links_workspace_id",
        table_name="security_ticket_links",
    )
    op.drop_table("security_ticket_links")
