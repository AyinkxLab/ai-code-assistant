"""Workspace activity event model and event-type catalog.

``ActivityEvent`` is the single data layer powering the team activity feed
(145), the owner-only audit view (151), and notification triggers (149).
Events are append-only: they are created alongside the action they describe and
are never updated or deleted by application code.

Security: ``metadata`` must never contain sensitive payloads (source file
contents, invitation tokens, passwords). It carries small, safe facts such as
role values and event labels. ``target_type``/``target_id`` reference the
affected object (e.g. ``user``/``123`` or ``project``/``45``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

# Event types used across the collaboration surfaces. Each event is recorded by
# the service that performs the action so actor/workspace/metadata stay correct.
EVENT_MEMBER_ADDED = "member.added"
EVENT_MEMBER_REMOVED = "member.removed"
EVENT_MEMBER_LEFT = "member.left"
EVENT_ROLE_CHANGED = "role.changed"
EVENT_OWNERSHIP_TRANSFERRED = "ownership.transferred"
EVENT_INVITATION_CREATED = "invitation.created"
EVENT_INVITATION_ACCEPTED = "invitation.accepted"
EVENT_INVITATION_DECLINED = "invitation.declined"
EVENT_INVITATION_CANCELLED = "invitation.cancelled"
EVENT_PROJECT_IMPORTED = "project.imported"
EVENT_PROJECT_DELETED = "project.deleted"
EVENT_AI_ANALYSIS_RUN = "ai.analysis.run"
EVENT_COMMENT_ADDED = "comment.added"
EVENT_SETTINGS_CHANGED = "settings.changed"
EVENT_PLUGIN_CAPABILITY_GRANTED = "plugin.capability.granted"
EVENT_PLUGIN_CAPABILITY_REVOKED = "plugin.capability.revoked"
EVENT_PLUGIN_ENABLED = "plugin.enabled"
EVENT_PLUGIN_DISABLED = "plugin.disabled"
EVENT_PLUGIN_DENIED = "plugin.denied"

# The audit subset: events that record security-relevant membership, permission,
# invitation, ownership, and plugin capability/state history. The owner-only
# audit view (151) reads exactly this set, so it is authoritative.
AUDIT_EVENT_TYPES = frozenset(
    {
        EVENT_MEMBER_ADDED,
        EVENT_MEMBER_REMOVED,
        EVENT_MEMBER_LEFT,
        EVENT_ROLE_CHANGED,
        EVENT_OWNERSHIP_TRANSFERRED,
        EVENT_INVITATION_CREATED,
        EVENT_INVITATION_ACCEPTED,
        EVENT_INVITATION_DECLINED,
        EVENT_INVITATION_CANCELLED,
        EVENT_PLUGIN_CAPABILITY_GRANTED,
        EVENT_PLUGIN_CAPABILITY_REVOKED,
        EVENT_PLUGIN_ENABLED,
        EVENT_PLUGIN_DISABLED,
        EVENT_PLUGIN_DENIED,
    }
)

# Human-readable labels for the feed/audit UI.
EVENT_LABELS = {
    EVENT_MEMBER_ADDED: "joined the workspace",
    EVENT_MEMBER_REMOVED: "was removed from the workspace",
    EVENT_MEMBER_LEFT: "left the workspace",
    EVENT_ROLE_CHANGED: "changed a member role",
    EVENT_OWNERSHIP_TRANSFERRED: "transferred workspace ownership",
    EVENT_INVITATION_CREATED: "invited someone",
    EVENT_INVITATION_ACCEPTED: "accepted an invitation",
    EVENT_INVITATION_DECLINED: "declined an invitation",
    EVENT_INVITATION_CANCELLED: "cancelled an invitation",
    EVENT_PROJECT_IMPORTED: "imported a project",
    EVENT_PROJECT_DELETED: "deleted a project",
    EVENT_AI_ANALYSIS_RUN: "ran an AI analysis",
    EVENT_COMMENT_ADDED: "commented on a project",
    EVENT_SETTINGS_CHANGED: "changed workspace settings",
    EVENT_PLUGIN_CAPABILITY_GRANTED: "granted a plugin capability",
    EVENT_PLUGIN_CAPABILITY_REVOKED: "revoked a plugin capability",
    EVENT_PLUGIN_ENABLED: "enabled a plugin",
    EVENT_PLUGIN_DISABLED: "disabled a plugin",
    EVENT_PLUGIN_DENIED: "denied a plugin action",
}


class ActivityEvent(db.Model):
    """A single recorded workspace activity event."""

    __tablename__ = "activity_events"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    event_type = db.Column(db.String(50), nullable=False, index=True)
    target_type = db.Column(db.String(50), nullable=True)
    target_id = db.Column(db.Integer, nullable=True)
    event_metadata = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        db.Index("ix_activity_events_workspace_created", "workspace_id", "created_at"),
    )

    workspace = db.relationship("Workspace")
    actor = db.relationship("User", foreign_keys=[actor_id])

    @property
    def label(self) -> str:
        """A human-readable description of the event type."""
        return EVENT_LABELS.get(self.event_type, self.event_type.replace(".", " "))

    def to_dict(self, *, actor_username: str | None = None) -> dict:
        """Serialize the event for API responses.

        ``metadata`` is only included when explicitly requested through the
        audit view (owner-only), never in the member activity feed.
        """
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "actor_id": self.actor_id,
            "actor_username": actor_username,
            "event_type": self.event_type,
            "label": self.label,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_audit_dict(self) -> dict:
        """Serialize including the (safe) metadata for the owner audit view."""
        payload = self.to_dict(actor_username=self.actor.username if self.actor else None)
        payload["metadata"] = self.event_metadata or {}
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ActivityEvent id={self.id} type={self.event_type!r}>"
