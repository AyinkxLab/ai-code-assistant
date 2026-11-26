"""Organization SSO session policy (#591).

A workspace owner (the organization anchor used elsewhere in the admin
console) may opt in to stricter browser-session rules. With no policy row the
existing session behaviour is unchanged, so this table is purely additive.
"""

from datetime import UTC, datetime

from app.extensions import db

DEFAULT_REAUTH_WINDOW_MINUTES = 15
MAX_MINUTES = 10080
MAX_REAUTH_WINDOW_MINUTES = 1440


class OrganizationSessionPolicy(db.Model):
    """Per-organization limits for tracked browser sessions."""

    __tablename__ = "organization_session_policies"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    #: ``None`` means "no new maximum"; limits are opt-in.
    max_session_age_minutes = db.Column(db.Integer, nullable=True)
    idle_timeout_minutes = db.Column(db.Integer, nullable=True)
    require_reauth_for_sensitive_actions = db.Column(db.Boolean, nullable=False, default=False)
    reauth_window_minutes = db.Column(
        db.Integer, nullable=False, default=DEFAULT_REAUTH_WINDOW_MINUTES
    )
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

    workspace = db.relationship("Workspace", back_populates="session_policy")

    def to_dict(self) -> dict:
        """Serialize policy settings; never includes secrets or tokens."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "max_session_age_minutes": self.max_session_age_minutes,
            "idle_timeout_minutes": self.idle_timeout_minutes,
            "require_reauth_for_sensitive_actions": bool(self.require_reauth_for_sensitive_actions),
            "reauth_window_minutes": self.reauth_window_minutes,
            "updated_by_id": self.updated_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrganizationSessionPolicy workspace_id={self.workspace_id}>"
