"""Encrypted LLM provider API key model.

A user may store their own provider keys (OpenAI, Anthropic, …). Only the
ciphertext produced by :func:`app.services.crypto.encrypt_api_key` is persisted;
the plaintext key is never stored, logged, or serialized. ``to_dict`` therefore
exposes only non-sensitive metadata.
"""

from datetime import UTC, datetime

from app.extensions import db

#: Providers offered by the UI scaffold. Any non-empty slug is accepted by the
#: API; this list only drives the dropdown.
COMMON_PROVIDERS = ("openai", "anthropic", "google", "azure", "openrouter", "custom")


class ApiKey(db.Model):
    """A user-owned, encrypted LLM provider API key."""

    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider = db.Column(db.String(50), nullable=False)
    # AES-256-GCM ciphertext (version-tagged + base64). Never plaintext.
    encrypted_value = db.Column(db.Text, nullable=False)
    label = db.Column(db.String(100), nullable=True)
    last_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
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

    def to_dict(self) -> dict:
        """Serialize metadata only — never the ciphertext or plaintext key."""
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "is_active": bool(self.is_active),
            "has_key": True,
            "last_verified_at": (
                self.last_verified_at.isoformat() if self.last_verified_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApiKey id={self.id} provider={self.provider!r}>"
