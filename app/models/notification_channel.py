"""Per-workspace notification channel preferences (#631).

Workspace owners choose which delivery channels (email, chat, webhook) carry
each event type. Rows are sparse: an event type without a row uses the defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

#: Event types that can be routed to channels.
EVENT_TYPES = (
    "invitation",
    "mention",
    "membership",
    "role_change",
    "ai_event",
    "share",
    "budget_alert",
    "billing_alert",
    "plugin_update",
)

#: Supported delivery channels.
CHANNELS = ("email", "chat", "webhook")

#: Default channel configuration for an unconfigured event type.
DEFAULT_CHANNELS = {"email": False, "chat": True, "webhook": False}


class NotificationChannelPreference(db.Model):
    """One event type's channel switches for a workspace."""

    __tablename__ = "notification_channel_preferences"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "event_type", name="uq_notification_channel_workspace_event"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(50), nullable=False)
    email = db.Column(db.Boolean, nullable=False, default=DEFAULT_CHANNELS["email"])
    chat = db.Column(db.Boolean, nullable=False, default=DEFAULT_CHANNELS["chat"])
    webhook = db.Column(db.Boolean, nullable=False, default=DEFAULT_CHANNELS["webhook"])
    updated_by_id = db.Column(
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

    def channels(self) -> dict:
        """Return the channel switches as a mapping."""
        return {"email": bool(self.email), "chat": bool(self.chat), "webhook": bool(self.webhook)}

    def to_dict(self) -> dict:
        """Serialize the row for the preferences API."""
        return {"event_type": self.event_type, **self.channels()}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NotificationChannelPreference workspace_id={self.workspace_id} "
            f"event_type={self.event_type!r}>"
        )
