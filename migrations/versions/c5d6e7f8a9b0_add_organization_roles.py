"""add organization roles and capabilities

Revision ID: c5d6e7f8a9b0
Revises: b4e2f1a3c5d7
Create Date: 2026-11-25 00:10:00.000000

Phase 11 (#597): organization-scoped custom roles use the plain capability
action namespace from ``app.services.permissions.CAPABILITIES``. Built-in
owner/admin/member roles are backfilled for every existing organization owner.
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "c5d6e7f8a9b0"
down_revision = "b4e2f1a3c5d7"
branch_labels = None
depends_on = None

# Mirrors app.services.organization_roles.BUILTIN_CAPABILITIES. Capabilities
# are plain actions, not the plugin "domain:action" strings.
BUILTIN_CAPABILITIES = {
    "owner": (
        "view_members",
        "manage_members",
        "manage_invitations",
        "manage_settings",
        "transfer_ownership",
        "view_audit",
        "comment",
        "view_activity",
        "heartbeat",
        "manage_plugins",
        "manage_billing",
        "manage_integrations",
    ),
    "admin": (
        "view_members",
        "comment",
        "view_activity",
        "heartbeat",
        "manage_members",
        "manage_invitations",
        "manage_settings",
        "view_audit",
    ),
    "member": ("view_members", "comment", "view_activity", "heartbeat", "leave_workspace"),
}


def upgrade():
    op.create_table(
        "organization_roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_organization_role_owner_name"),
    )
    op.create_table(
        "organization_role_capabilities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["organization_roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_id", "capability", name="uq_organization_role_capability"),
    )
    with op.batch_alter_table("organization_roles", schema=None) as batch_op:
        batch_op.create_index("ix_organization_roles_owner_id", ["owner_id"], unique=False)
    with op.batch_alter_table("organization_role_capabilities", schema=None) as batch_op:
        batch_op.create_index(
            "ix_organization_role_capabilities_role_id", ["role_id"], unique=False
        )

    # Backfill one built-in set per existing organization owner. The service
    # performs the same seeding for databases created with db.create_all().
    bind = op.get_bind()
    users = sa.table("users", sa.column("id", sa.Integer()))
    roles = sa.table(
        "organization_roles",
        sa.column("id", sa.Integer()),
        sa.column("owner_id", sa.Integer()),
        sa.column("name", sa.String(length=64)),
        sa.column("description", sa.String(length=300)),
        sa.column("is_builtin", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    grants = sa.table(
        "organization_role_capabilities",
        sa.column("role_id", sa.Integer()),
        sa.column("capability", sa.String(length=64)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    for user_id in bind.execute(sa.select(users.c.id)).scalars():
        for name, capabilities in BUILTIN_CAPABILITIES.items():
            bind.execute(
                roles.insert().values(
                    owner_id=user_id,
                    name=name,
                    description=f"Built-in {name} role.",
                    is_builtin=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            role_id = bind.execute(
                sa.select(roles.c.id).where(roles.c.owner_id == user_id, roles.c.name == name)
            ).scalar_one()
            bind.execute(
                grants.insert(),
                [
                    {"role_id": role_id, "capability": cap, "created_at": now}
                    for cap in capabilities
                ],
            )


def downgrade():
    with op.batch_alter_table("organization_role_capabilities", schema=None) as batch_op:
        batch_op.drop_index("ix_organization_role_capabilities_role_id")
    with op.batch_alter_table("organization_roles", schema=None) as batch_op:
        batch_op.drop_index("ix_organization_roles_owner_id")
    op.drop_table("organization_role_capabilities")
    op.drop_table("organization_roles")
