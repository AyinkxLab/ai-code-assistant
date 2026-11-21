"""Email unsubscribe suppression list (#682).

A row suppresses outbound integration mail for one workspace/recipient pair.
Recipients are stored exactly as normalized (lowercased, trimmed); no message
content or secret is retained. Unsubscribe links carry an HMAC-signed token so a
recipient cannot suppress another address by guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db


class EmailSuppression(db.Model):
    """A recipient that opted out of workspace integration email."""

    __tablename__ = "email_suppressions"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id",
            "email",
            name="uq_email_suppressions_workspace_email",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email = db.Column(db.String(255), nullable=False, index=True)
    reason = db.Column(db.String(50), nullable=False, default="unsubscribe")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict:
        """Serialize the suppression without exposing unrelated data."""
        return {
            "workspace_id": self.workspace_id,
            "email": self.email,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EmailSuppression workspace_id={self.workspace_id} email={self.email!r}>"
