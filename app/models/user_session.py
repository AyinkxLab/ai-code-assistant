"""Tracked login sessions for central revocation (#619).

Flask-Login signs the session cookie but has no server-side record, so a stolen
or shared cookie cannot be invalidated. Each successful login now creates one
:class:`UserSession` row and stores its opaque token in the signed cookie. The
request hook in :mod:`app.auth.routes` refuses a cookie whose row is missing or
revoked, which makes "revoke all" take effect on the next request.
"""

from datetime import UTC, datetime

from app.extensions import db

#: Maximum stored user-agent prefix; only used for display.
MAX_USER_AGENT_LENGTH = 255


class UserSession(db.Model):
    """One server-side login session belonging to a user."""

    __tablename__ = "user_sessions"

    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_seen_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoke_reason = db.Column(db.String(255), nullable=True)
    user_agent = db.Column(db.String(MAX_USER_AGENT_LENGTH), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])

    @property
    def is_active(self) -> bool:
        """Return ``True`` while the session has not been revoked."""
        return self.revoked_at is None

    def to_dict(self) -> dict:
        """Serialize the session for the account security page."""
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoke_reason": self.revoke_reason,
            "user_agent": self.user_agent,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserSession id={self.id!r} user_id={self.user_id} active={self.is_active}>"
