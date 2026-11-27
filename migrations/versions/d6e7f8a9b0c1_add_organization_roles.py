"""add organization roles and the role migration undo ledger

Revision ID: d6e7f8a9b0c1
Revises: b4e2f1a3c5d7
Create Date: 2026-11-27

Phase 11 (#605): store capability-backed role definitions per organization
owner and seed the four built-in roles. The companion script
``scripts/role_migration.py`` translates legacy membership strings using the
``role_migration_records`` undo ledger.
"""

from datetime import UTC, datetime
import json

import sqlalchemy as sa
from alembic import op

revision = "d6e7f8a9b0c1"
down_revision = "b4e2f1a3c5d7"
branch_labels = None
depends_on = None

# Mirrors BUILTIN_ROLE_CAPABILITIES in app/services/role_migration.py. Kept as
# literals so a migrated database never depends on future application changes.
BUILTIN_ROLE_CAPABILITIES = {
    "viewer": [
        "comment",
        "heartbeat",
        "leave_workspace",
        "view_activity",
        "view_members",
    ],
    "contributor": [
        "comment",
        "heartbeat",
        "leave_workspace",
        "view_activity",
        "view_members",
    ],
    # An organization admin does not widen the workspace capability matrix.
    "admin": [],
    "owner": [
        "comment",
        "heartbeat",
        "manage_billing",
        "manage_integrations",
        "manage_invitations",
        "manage_members",
        "manage_plugins",
        "manage_settings",
        "transfer_ownership",
        "view_activity",
        "view_audit",
        "view_members",
    ],
}


def upgrade():
    op.create_table(
        "organization_roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("capabilities", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_org_roles_owner_name"),
        sa.Index("ix_org_roles_owner_id", "owner_id"),
    )
    op.create_table(
        "role_migration_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("before_state", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("after_state", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_roles", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_role_migration_ws_user"),
        sa.Index("ix_role_migration_user_id", "user_id"),
    )

    bind = op.get_bind()
    owner_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT DISTINCT user_id FROM workspaces WHERE user_id IS NOT NULL")
        )
    ]
    now = datetime.now(UTC)
    rows = [
        {
            "owner_id": owner_id,
            "name": name,
            "description": f"Built-in {name} role",
            "capabilities": json.dumps(capabilities, sort_keys=True),
            "is_builtin": True,
            "created_by_id": None,
            "updated_by_id": None,
            "created_at": now,
            "updated_at": now,
        }
        for owner_id in sorted(owner_ids)
        for name, capabilities in BUILTIN_ROLE_CAPABILITIES.items()
    ]
    if rows:
        op.bulk_insert(
            sa.Table(
                "organization_roles",
                sa.MetaData(),
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("owner_id", sa.Integer()),
                sa.Column("name", sa.String(length=64)),
                sa.Column("description", sa.String(length=255)),
                sa.Column("capabilities", sa.Text()),
                sa.Column("is_builtin", sa.Boolean()),
                sa.Column("created_by_id", sa.Integer()),
                sa.Column("updated_by_id", sa.Integer()),
                sa.Column("created_at", sa.DateTime(timezone=True)),
                sa.Column("updated_at", sa.DateTime(timezone=True)),
            ),
            rows,
        )


def downgrade():
    op.drop_table("role_migration_records")
    op.drop_table("organization_roles")
