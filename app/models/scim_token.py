"""Organization SCIM bearer tokens (#594).

A SCIM token is scoped to an organization owner (``Workspace.user_id``) and has
a default provisioning workspace. Like personal API tokens, only a SHA-256 hash
of the secret is stored; the full secret is returned exactly once when the token
is issued or rotated. ``to_dict`` exposes display metadata only.
"""

from datetime import UTC, datetime

from app.extensions import db


class ScimToken(db.Model):
    """One organization-level bearer token for the SCIM 2.0 API."""

    __tablename__ = "scim_tokens"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Default target workspace for SCIM provisioning requests.
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = db.Column(db.String(100), nullable=True)
    #: First characters of the secret, used only to tell tokens apart.
    prefix = db.Column(db.String(16), nullable=False)
    #: SHA-256 of the full secret; the secret itself is never persisted.
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    @property
    def is_active(self) -> bool:
        """Return ``True`` when the token is neither revoked nor expired."""
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            expires = self.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                return False
        return True

    def to_dict(self) -> dict:
        """Serialize metadata only — never the secret or its hash."""
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "workspace_id": self.workspace_id,
            "label": self.label,
            "prefix": self.prefix,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "is_active": self.is_active,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ScimToken id={self.id} owner_id={self.owner_id} prefix={self.prefix!r}>"
