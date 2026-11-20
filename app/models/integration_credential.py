"""Encrypted third-party integration credentials (#683).

Every integration secret is stored with the same authenticated encryption used
for user-owned provider API keys: AES-256-GCM with a fresh random nonce and a
version tag (see :mod:`app.services.crypto`). The plaintext exists only in
memory for the duration of a call.

Serialization is deliberately metadata-only: neither the ciphertext nor the
plaintext is exposed by :meth:`IntegrationCredential.to_dict`, and the ``repr``
never includes either.
"""

from datetime import UTC, datetime

from app.extensions import db


class IntegrationCredential(db.Model):
    """A workspace-scoped encrypted secret for one integration."""

    __tablename__ = "integration_credentials"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id",
            "integration",
            "name",
            name="uq_integration_credentials_workspace_integration_name",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Integration slug, for example ``github`` or ``stellar``.
    integration = db.Column(db.String(50), nullable=False, index=True)
    #: Credential name within the integration, for example ``access_token``.
    name = db.Column(db.String(100), nullable=False)
    #: AES-256-GCM ciphertext (version-tagged + base64). Never plaintext.
    encrypted_value = db.Column(db.Text, nullable=False)
    last_rotated_at = db.Column(db.DateTime(timezone=True), nullable=True)
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
        """Return non-sensitive metadata for API responses."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "integration": self.integration,
            "name": self.name,
            "has_secret": bool(self.encrypted_value),
            "last_rotated_at": (self.last_rotated_at.isoformat() if self.last_rotated_at else None),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<IntegrationCredential id={self.id} workspace_id={self.workspace_id} "
            f"integration={self.integration!r} name={self.name!r}>"
        )
