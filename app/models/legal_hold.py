"""Legal holds for workspace and audit data (#609).

A legal hold is an explicit, time-stamped instruction to preserve data while an
investigation, litigation, or regulatory request is open. Unlike the legacy
``audit_retention_policies.legal_hold`` boolean (kept for compatibility), a row
records the scope, the reason, and the actor who placed it, and it is released
by recording who released it and why.
"""

from datetime import UTC, datetime

from app.extensions import db

SCOPE_AUDIT = "audit"
SCOPE_WORKSPACE = "workspace"
SCOPE_ALL = "all"
SCOPES = (SCOPE_AUDIT, SCOPE_WORKSPACE, SCOPE_ALL)

#: Hard cap for a human-readable hold/release reason.
MAX_REASON_LENGTH = 2000


class LegalHold(db.Model):
    """One hold covering a workspace; active until ``released_at`` is set."""

    __tablename__ = "legal_holds"
    __table_args__ = (db.Index("ix_legal_holds_workspace_active", "workspace_id", "released_at"),)

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope = db.Column(db.String(16), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
    released_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    release_reason = db.Column(db.Text, nullable=True)

    workspace = db.relationship("Workspace")

    @property
    def is_active(self) -> bool:
        """Return ``True`` while the hold has not been released."""
        return self.released_at is None

    def covers(self, scope: str) -> bool:
        """Return ``True`` when this active hold protects ``scope``."""
        return self.is_active and self.scope in (scope, SCOPE_ALL)

    def to_dict(self) -> dict:
        """Serialize the hold for API/CLI output."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "scope": self.scope,
            "reason": self.reason,
            "actor_id": self.actor_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "released_by_id": self.released_by_id,
            "release_reason": self.release_reason,
            "active": self.is_active,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<LegalHold id={self.id} workspace_id={self.workspace_id} "
            f"scope={self.scope!r} active={self.is_active}>"
        )
