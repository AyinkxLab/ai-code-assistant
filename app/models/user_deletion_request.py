"""User data deletion / anonymization request workflow (#615).

One row per request. The state machine is:

``requested`` -> ``confirmed`` -> ``completed``

A request may also be ``cancelled`` before it is completed. ``mode`` selects
what completion does:

* ``deletion`` — delete the user's personal content, then anonymize the
  account profile (see :mod:`app.services.data_deletion`).
* ``anonymization`` — anonymize the account profile and keep content.

Audit rows are never deleted: the account row is retained (disabled and
anonymized) so the append-only audit trail keeps its actor and workspace
references.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

STATUS_REQUESTED = "requested"
STATUS_CONFIRMED = "confirmed"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

STATUSES = (STATUS_REQUESTED, STATUS_CONFIRMED, STATUS_COMPLETED, STATUS_CANCELLED)

MODE_DELETION = "deletion"
MODE_ANONYMIZATION = "anonymization"
MODES = (MODE_DELETION, MODE_ANONYMIZATION)


class UserDeletionRequest(db.Model):
    """One account deletion/anonymization request and its lifecycle."""

    __tablename__ = "user_deletion_requests"
    __table_args__ = (
        db.Index("ix_user_deletion_requests_user_requested", "user_id", "requested_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(db.String(20), nullable=False, default=STATUS_REQUESTED, index=True)
    mode = db.Column(db.String(20), nullable=False, default=MODE_DELETION)
    requested_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cancelled_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship("User")

    def to_dict(self) -> dict:
        """Serialize request metadata (no personal content)."""
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<UserDeletionRequest id={self.id} user_id={self.user_id} "
            f"status={self.status!r} mode={self.mode!r}>"
        )
