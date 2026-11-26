"""Workspace membership model.

The collaboration foundation: a workspace is owned by the user in
``Workspace.user_id`` and can additionally list members with a role. Phase 6
shipped the membership data model and owner-only management API (add/update/
remove members); Phase 7 (Team Collaboration) adds the lifecycle:

* ``status`` tracks ``active`` / ``removed`` so history survives soft removal.
* ``joined_at`` / ``removed_at`` record when a member joined and left.
* ``last_seen_at`` feeds the presence badge (online / active N ago).
* A member can leave themselves (``DELETE .../membership``); the owner cannot
  leave and must transfer ownership first.

A user who is neither the owner nor an active member cannot access the
workspace. ``removed`` rows never appear in member listings and never grant
access.
"""

from datetime import UTC, datetime

from app.extensions import db
from app.models.group_role_mapping import PROVISIONING_MANUAL

ROLE_OWNER = "owner"
ROLE_CONTRIBUTOR = "contributor"
ROLE_VIEWER = "viewer"
VALID_ROLES = (ROLE_OWNER, ROLE_CONTRIBUTOR, ROLE_VIEWER)
# Roles assignable to non-owner members.
MEMBER_ROLES = (ROLE_CONTRIBUTOR, ROLE_VIEWER)

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
VALID_STATUSES = (STATUS_ACTIVE, STATUS_REMOVED)


class WorkspaceMember(db.Model):
    """A user granted access to a workspace with a specific role."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_ws_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False, default=ROLE_VIEWER)
    #: ``manual`` memberships are never removed by SCIM group changes (#593).
    provisioning_source = db.Column(db.String(16), nullable=False, default=PROVISIONING_MANUAL)
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE)
    joined_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    removed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    workspace = db.relationship("Workspace", back_populates="members")
    user = db.relationship("User")

    @property
    def is_active_member(self) -> bool:
        """Return ``True`` when the membership currently grants access."""
        return self.status == STATUS_ACTIVE

    @property
    def last_active_at(self) -> datetime | None:
        """Best-available presence timestamp: heartbeat, else last login."""
        if self.last_seen_at:
            return self.last_seen_at
        if self.user is not None:
            return self.user.last_login_at
        return None

    def mark_removed(self) -> None:
        """Soft-remove this membership, preserving history."""
        self.status = STATUS_REMOVED
        self.removed_at = datetime.now(UTC)

    def reactivate(self) -> None:
        """Restore a previously removed membership to active."""
        self.status = STATUS_ACTIVE
        self.removed_at = None

    def to_dict(self) -> dict:
        """Serialize the membership for JSON API responses."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "role": self.role,
            "status": self.status,
            "username": self.user.username if self.user else None,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "removed_at": self.removed_at.isoformat() if self.removed_at else None,
            "last_active_at": self.last_active_at.isoformat() if self.last_active_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkspaceMember id={self.id} workspace_id={self.workspace_id} role={self.role!r}>"
