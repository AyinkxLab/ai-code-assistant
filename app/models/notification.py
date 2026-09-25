"""Notification model and notification-type catalog.

A persisted per-user notification produced through the single ``notify()``
service. Notification types are shared with the notification wiring (149) and
the preference model (150) so opt-outs map 1:1 to the types the app produces.

Security: ``payload`` must never contain secrets, invitation tokens, or another
user's data. Users can only ever read their own notifications (enforced by the
notification API which always filters on ``current_user.id``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

NOTIF_INVITATION = "invitation"
NOTIF_MENTION = "mention"
NOTIF_MEMBERSHIP = "membership"
NOTIF_ROLE_CHANGE = "role_change"
NOTIF_AI_EVENT = "ai_event"
NOTIF_SHARE = "share"

# Notification types that are always delivered: users cannot disable security-
# critical system notifications about their own access.
NOTIF_TYPES = (
    NOTIF_INVITATION,
    NOTIF_MENTION,
    NOTIF_MEMBERSHIP,
    NOTIF_ROLE_CHANGE,
    NOTIF_AI_EVENT,
    NOTIF_SHARE,
)


class Notification(db.Model):
    """A single notification for one user."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type = db.Column(db.String(30), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    payload = db.Column(db.JSON, nullable=True)
    link = db.Column(db.String(500), nullable=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        db.Index("ix_notifications_user_read_created", "user_id", "is_read", "created_at"),
    )

    actor = db.relationship("User", foreign_keys=[actor_id])

    def to_dict(self) -> dict:
        """Serialize the notification for the inbox API."""
        return {
            "id": self.id,
            "type": self.type,
            "actor_id": self.actor_id,
            "actor_username": self.actor.username if self.actor else None,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "payload": self.payload or {},
            "link": self.link,
            "is_read": self.is_read,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Notification id={self.id} user_id={self.user_id} type={self.type!r}>"
