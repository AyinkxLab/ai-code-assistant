"""Notification preference model.

Per-user opt-in/opt-out switches for each notification category. Defaults are
permissive (all on) for new users. Security-critical system notifications
(invitations, role changes, membership changes) can be turned off by design
choice here because the app still surfaces them in the activity/audit surfaces;
the notification service honors these switches when creating notifications.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

PREF_INVITATIONS = "invitations"
PREF_MENTIONS = "mentions"
PREF_MEMBERSHIP = "membership"
PREF_AI_EVENTS = "ai_events"
PREF_SHARES = "shares"

PREFERENCE_TYPES = (
    PREF_INVITATIONS,
    PREF_MENTIONS,
    PREF_MEMBERSHIP,
    PREF_AI_EVENTS,
    PREF_SHARES,
)

# Map notification type -> preference key. ``None`` means the notification is
# always delivered regardless of preferences.
TYPE_PREFERENCE_MAP = {
    "invitation": PREF_INVITATIONS,
    "mention": PREF_MENTIONS,
    "membership": PREF_MEMBERSHIP,
    "role_change": None,
    "ai_event": PREF_AI_EVENTS,
    "share": PREF_SHARES,
}


class NotificationPreference(db.Model):
    """Per-user settings controlling which notification categories are sent."""

    __tablename__ = "notification_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    invitations = db.Column(db.Boolean, nullable=False, default=True)
    mentions = db.Column(db.Boolean, nullable=False, default=True)
    membership = db.Column(db.Boolean, nullable=False, default=True)
    ai_events = db.Column(db.Boolean, nullable=False, default=True)
    shares = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def to_dict(self) -> dict:
        """Serialize the preferences for the API."""
        return {
            "invitations": self.invitations,
            "mentions": self.mentions,
            "membership": self.membership,
            "ai_events": self.ai_events,
            "shares": self.shares,
        }

    def allows(self, notification_type: str) -> bool:
        """Return ``True`` when ``notification_type`` may be delivered."""
        pref_key = TYPE_PREFERENCE_MAP.get(notification_type)
        if pref_key is None:
            return True
        return bool(getattr(self, pref_key, True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<NotificationPreference user_id={self.user_id}>"
