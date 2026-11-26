"""Organization-defined roles and their capabilities (#597).

There is no separate organizations table: an organization is anchored on its
workspace owner (``Workspace.user_id``), exactly as in
:mod:`app.models.organization_plugin_policy`. A role therefore belongs to an
``owner_id`` that references ``users.id``, and its name is unique per owner.

Capabilities use the plain action namespace of
``app.services.permissions.CAPABILITIES`` (for example ``manage_members``), not
the plugin capability strings with a ``domain:action`` prefix. Built-in roles
are created by the migration and by :func:`ensure_builtin_roles` for databases
created with ``db.create_all()``; they cannot be renamed or deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

MAX_ROLE_NAME_LENGTH = 64
MAX_CAPABILITY_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 300

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
BUILTIN_ROLE_NAMES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)


class OrganizationRole(db.Model):
    """A named, reusable set of capabilities for one organization."""

    __tablename__ = "organization_roles"
    __table_args__ = (
        db.UniqueConstraint("owner_id", "name", name="uq_organization_role_owner_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(MAX_ROLE_NAME_LENGTH), nullable=False)
    description = db.Column(db.String(MAX_DESCRIPTION_LENGTH), nullable=True)
    #: Built-ins are seeded by the migration and are immutable from the UI.
    is_builtin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    capabilities = db.relationship(
        "OrganizationRoleCapability",
        backref="role",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def capability_names(self) -> list[str]:
        """Return the granted actions in stable order."""
        return sorted(row.capability for row in self.capabilities)

    def to_dict(self) -> dict:
        """Serialize the role for API output (no secrets are stored here)."""
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "name": self.name,
            "description": self.description,
            "is_builtin": bool(self.is_builtin),
            "capabilities": self.capability_names,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrganizationRole id={self.id} owner_id={self.owner_id} name={self.name!r}>"


class OrganizationRoleCapability(db.Model):
    """One capability granted to an organization role."""

    __tablename__ = "organization_role_capabilities"
    __table_args__ = (
        db.UniqueConstraint("role_id", "capability", name="uq_organization_role_capability"),
    )

    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    capability = db.Column(db.String(MAX_CAPABILITY_LENGTH), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict:
        """Serialize the grant for API output."""
        return {"id": self.id, "role_id": self.role_id, "capability": self.capability}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrganizationRoleCapability role_id={self.role_id} capability={self.capability!r}>"


# A short alias keeps callers that use the generic name working.
RoleCapability = OrganizationRoleCapability

__all__ = [
    "BUILTIN_ROLE_NAMES",
    "MAX_CAPABILITY_LENGTH",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_ROLE_NAME_LENGTH",
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "ROLE_OWNER",
    "OrganizationRole",
    "OrganizationRoleCapability",
    "RoleCapability",
]
