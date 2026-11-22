"""User data export request workflow (#614).

One row per request. The state machine is:

``requested`` -> ``processing`` -> ``ready`` -> ``downloaded``

A ``ready`` request whose download window has passed becomes ``expired``. The
raw download token is returned once by :mod:`app.services.data_export`; only its
SHA-256 hash is stored here, so the database never contains a usable link.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.extensions import db

STATUS_REQUESTED = "requested"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_DOWNLOADED = "downloaded"
STATUS_EXPIRED = "expired"

STATUSES = (
    STATUS_REQUESTED,
    STATUS_PROCESSING,
    STATUS_READY,
    STATUS_DOWNLOADED,
    STATUS_EXPIRED,
)


def hash_download_token(token: str) -> str:
    """Return the SHA-256 hex digest of a download token."""
    if not isinstance(token, str) or not token:
        raise ValueError("download token must be a non-empty string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserDataExport(db.Model):
    """One account-data export request and its lifecycle timestamps."""

    __tablename__ = "user_data_exports"
    __table_args__ = (db.Index("ix_user_data_exports_user_requested", "user_id", "requested_at"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(db.String(20), nullable=False, default=STATUS_REQUESTED, index=True)
    #: Hash of the one-time download token. The plaintext token is never stored.
    token_hash = db.Column(db.String(64), nullable=True, unique=True, index=True)
    requested_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    processing_started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    downloaded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    user = db.relationship("User")

    def _downloadable(self, now: datetime) -> bool:
        if self.status != STATUS_READY:
            return False
        if self.expires_at is None:
            return True
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > now

    def to_dict(self) -> dict:
        """Serialize request metadata without the token or its hash."""
        return {
            "id": self.id,
            "status": self.status,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "processing_started_at": (
                self.processing_started_at.isoformat() if self.processing_started_at else None
            ),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "downloaded_at": self.downloaded_at.isoformat() if self.downloaded_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "downloadable": self._downloadable(datetime.now(UTC)),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserDataExport id={self.id} user_id={self.user_id} status={self.status!r}>"
