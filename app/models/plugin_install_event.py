"""Privacy-safe marketplace install analytics (#695).

Each row is an anonymous counter increment: plugin id, action and timestamp.
It deliberately has **no** user, workspace, IP address or user-agent column, so
install analytics can never be tied back to a person.
"""

from datetime import UTC, datetime

from app.extensions import db

ACTION_INSTALL = "install"
ACTION_UPDATE = "update"
ACTION_ROLLBACK = "rollback"
ACTIONS = (ACTION_INSTALL, ACTION_UPDATE, ACTION_ROLLBACK)


class PluginInstallEvent(db.Model):
    """One anonymous install/update/rollback event for a plugin."""

    __tablename__ = "plugin_install_events"

    id = db.Column(db.Integer, primary_key=True)
    plugin_id = db.Column(db.String(64), nullable=False, index=True)
    action = db.Column(db.String(16), nullable=False, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    def to_dict(self) -> dict:
        """Serialize the event; identity fields intentionally do not exist."""
        return {
            "id": self.id,
            "plugin_id": self.plugin_id,
            "action": self.action,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PluginInstallEvent plugin={self.plugin_id!r} action={self.action!r}>"
