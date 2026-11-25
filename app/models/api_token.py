"""Scoped API access tokens (#616).

A token authenticates public API requests as its owning user. Only a SHA-256
hash of the secret is stored; ``to_dict`` exposes display metadata (label,
prefix, scopes, timestamps) and never the secret or its hash. Scopes form a
ranked set — ``read`` < ``write`` < ``admin`` — enforced per request in
:mod:`app.services.api_tokens`.
"""

from datetime import UTC, datetime

from app.extensions import db

READ = "read"
WRITE = "write"
ADMIN = "admin"

SCOPES = (READ, WRITE, ADMIN)
SCOPE_RANK = {scope: index for index, scope in enumerate(SCOPES)}


class ApiToken(db.Model):
    """One personal API token owned by a user."""

    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label = db.Column(db.String(100), nullable=True)
    #: First characters of the secret, shown in the UI so a user can tell
    #: tokens apart without revealing the secret itself.
    prefix = db.Column(db.String(16), nullable=False)
    #: SHA-256 of the full secret. The secret is never stored.
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    #: Canonical comma-separated scopes, e.g. ``"read,project:write"``.
    #: Legacy ranked scopes come first, followed by sorted capability scopes.
    scopes = db.Column(db.String(512), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    @property
    def scope_list(self) -> list[str]:
        """Return granted scopes: legacy ranks first, then capability names."""
        granted = {part.strip() for part in (self.scopes or "").split(",") if part.strip()}
        legacy = [scope for scope in SCOPES if scope in granted]
        capabilities = sorted(scope for scope in granted if scope not in SCOPES and ":" in scope)
        return legacy + capabilities

    @property
    def capability_scopes(self) -> list[str]:
        """Return only the capability scopes granted to this token."""
        return [scope for scope in self.scope_list if scope not in SCOPES]

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
            "label": self.label,
            "prefix": self.prefix,
            "scopes": self.scope_list,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "is_active": self.is_active,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApiToken id={self.id} user_id={self.user_id} prefix={self.prefix!r}>"
