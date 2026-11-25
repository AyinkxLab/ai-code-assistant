"""Custom roles and assignments for organization, team, and workspace scopes (#599).

A custom role starts from a built-in non-owner role and applies an explicit grant
and deny list over the workspace capability matrix. Assignments are one per
(scope, scope target, user): reassigning replaces the previous role rather than
unioning capabilities.
"""

from datetime import UTC, datetime

from app.extensions import db

SCOPE_ORGANIZATION = "organization"
SCOPE_TEAM = "team"
SCOPE_WORKSPACE = "workspace"
SCOPES = (SCOPE_ORGANIZATION, SCOPE_TEAM, SCOPE_WORKSPACE)

BASE_ROLES = ("viewer", "contributor")
STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
STATUS_VALUES = (STATUS_ACTIVE, STATUS_REMOVED)


class CustomRole(db.Model):
    """A named, least-privilege capability set at one assignment scope."""

    __tablename__ = "custom_roles"
    __table_args__ = (
        db.UniqueConstraint("scope", "scope_id", "name", name="uq_custom_roles_scope_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(20), nullable=False, index=True)
    scope_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    base_role = db.Column(db.String(20), nullable=False, default="viewer")
    granted_capabilities = db.Column(db.JSON, nullable=False, default=list)
    denied_capabilities = db.Column(db.JSON, nullable=False, default=list)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def capability_set(self) -> set[str]:
        """Return the effective workspace capabilities for this role."""
        from app.services.permissions import CAPABILITIES, role_can

        capabilities = {cap for cap in CAPABILITIES if role_can(self.base_role, cap)}
        capabilities.update(self.granted_capabilities or [])
        capabilities.difference_update(self.denied_capabilities or [])
        return capabilities

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "name": self.name,
            "base_role": self.base_role,
            "granted_capabilities": sorted(self.granted_capabilities or []),
            "denied_capabilities": sorted(self.denied_capabilities or []),
            "active": self.active,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CustomRole id={self.id} scope={self.scope!r} name={self.name!r}>"


class RoleAssignment(db.Model):
    """One active custom-role assignment for a user at a scope target."""

    __tablename__ = "role_assignments"
    __table_args__ = (
        db.UniqueConstraint(
            "scope", "scope_id", "user_id", name="uq_role_assignment_scope_target_user"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(
        db.Integer, db.ForeignKey("custom_roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope = db.Column(db.String(20), nullable=False, index=True)
    scope_id = db.Column(db.Integer, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE)
    assigned_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    role = db.relationship("CustomRole", lazy="joined")
    user = db.relationship("User", foreign_keys=[user_id], lazy="select")

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE and self.role is not None and self.role.active

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role_id": self.role_id,
            "role_name": self.role.name if self.role else None,
            "user_id": self.user_id,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "status": self.status,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RoleAssignment id={self.id} scope={self.scope!r} user_id={self.user_id}>"
