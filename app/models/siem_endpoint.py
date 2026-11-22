"""Organization-configured SIEM forwarding endpoint (#629).

One endpoint per workspace. The signing secret is stored alongside a SHA-256
hash for rotation checks; it is never serialized by :meth:`to_dict`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.extensions import db


def hash_siem_secret(secret: str) -> str:
    """Return the SHA-256 hex digest of a SIEM signing secret."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("SIEM secret must be a non-empty string")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class SiemEndpoint(db.Model):
    """A workspace's outbound SIEM webhook configuration."""

    __tablename__ = "siem_endpoints"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    url = db.Column(db.String(2000), nullable=False)
    #: HMAC signing secret. Never serialized in any response.
    secret = db.Column(db.String(255), nullable=False)
    secret_hash = db.Column(db.String(64), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    workspace = db.relationship("Workspace")

    def set_secret(self, secret: str) -> None:
        """Store the signing secret and its hash."""
        self.secret_hash = hash_siem_secret(secret)
        self.secret = secret

    def signing_secret(self) -> str:
        """Return the plaintext signing secret."""
        return self.secret

    def to_dict(self) -> dict:
        """Serialize the endpoint without its secret or hash."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "url": self.url,
            "is_active": bool(self.is_active),
            "has_secret": bool(self.secret or self.secret_hash),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SiemEndpoint workspace_id={self.workspace_id} active={self.is_active}>"
