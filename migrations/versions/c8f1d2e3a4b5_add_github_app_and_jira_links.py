"""add GitHub App installations and Jira issue links

Revision ID: c8f1d2e3a4b5
Revises: b6c7d8e9f0a1
Create Date: 2026-11-21 00:00:00.000000

Phase 12: workspace-scoped GitHub App installation state (#676) and durable
Jira issue links that prevent duplicate exports of review findings (#680).
Secret installation tokens are stored encrypted; Jira credentials continue to
live in ``integration_credentials``.
"""

import sqlalchemy as sa
from alembic import op

revision = "c8f1d2e3a4b5"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "github_app_installations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("account_login", sa.String(length=200), nullable=False),
        sa.Column("account_type", sa.String(length=50), nullable=False),
        sa.Column("repository_selection", sa.String(length=20), nullable=False),
        sa.Column("repository_ids_json", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "installation_id",
            name="uq_github_app_installations_installation",
        ),
    )
    op.create_index(
        "ix_github_app_installations_workspace_id",
        "github_app_installations",
        ["workspace_id"],
    )
    op.create_index(
        "ix_github_app_installations_user_id",
        "github_app_installations",
        ["user_id"],
    )
    op.create_index(
        "ix_github_app_installations_workspace_status",
        "github_app_installations",
        ["workspace_id", "status"],
    )

    op.create_table(
        "jira_issue_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("finding_id", sa.Integer(), nullable=False),
        sa.Column("project_key", sa.String(length=50), nullable=False),
        sa.Column("external_key", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("priority", sa.String(length=30), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["finding_id"], ["review_findings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "finding_id",
            name="uq_jira_issue_links_workspace_finding",
        ),
        sa.UniqueConstraint("external_key", name="uq_jira_issue_links_external_key"),
    )
    op.create_index("ix_jira_issue_links_workspace_id", "jira_issue_links", ["workspace_id"])
    op.create_index("ix_jira_issue_links_review_id", "jira_issue_links", ["review_id"])
    op.create_index("ix_jira_issue_links_finding_id", "jira_issue_links", ["finding_id"])
    op.create_index("ix_jira_issue_links_external_key", "jira_issue_links", ["external_key"])
    op.create_index(
        "ix_jira_issue_links_workspace_created",
        "jira_issue_links",
        ["workspace_id", "created_at"],
    )


def downgrade():
    op.drop_index(
        "ix_jira_issue_links_workspace_created", table_name="jira_issue_links"
    )
    op.drop_index("ix_jira_issue_links_external_key", table_name="jira_issue_links")
    op.drop_index("ix_jira_issue_links_finding_id", table_name="jira_issue_links")
    op.drop_index("ix_jira_issue_links_review_id", table_name="jira_issue_links")
    op.drop_index("ix_jira_issue_links_workspace_id", table_name="jira_issue_links")
    op.drop_table("jira_issue_links")

    op.drop_index(
        "ix_github_app_installations_workspace_status",
        table_name="github_app_installations",
    )
    op.drop_index(
        "ix_github_app_installations_user_id", table_name="github_app_installations"
    )
    op.drop_index(
        "ix_github_app_installations_workspace_id", table_name="github_app_installations"
    )
    op.drop_table("github_app_installations")
