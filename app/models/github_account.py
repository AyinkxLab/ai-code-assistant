"""GitHub connection model.

Stores a user's GitHub OAuth connection. The access token is never persisted
in plain text; only an encrypted ciphertext produced by
:mod:`app.services.crypto` is stored.
"""

from datetime import UTC, datetime

from app.extensions import db


class GithubAccount(db.Model):
    """A user's connected GitHub account.

    ``access_token_encrypted`` holds the Fernet ciphertext of the access token
    used to call the GitHub REST API. The plaintext token is only materialized
    in memory, per request, inside the GitHub client.
    """

    __tablename__ = "github_accounts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    github_user_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    github_username = db.Column(db.String(80), nullable=False, index=True)
    access_token_encrypted = db.Column(db.Text, nullable=False)
    refresh_token_encrypted = db.Column(db.Text, nullable=True)
    token_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    token_type = db.Column(db.String(32), nullable=False, default="bearer")
    scopes = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def set_access_token(self, plaintext: str) -> None:
        """Encrypt and store ``plaintext`` (never kept in plain text)."""
        from app.services.crypto import encrypt_secret

        self.access_token_encrypted = encrypt_secret(plaintext)

    def set_refresh_token(self, plaintext: str | None) -> None:
        """Encrypt and store a refresh token, or clear it when absent."""
        from app.services.crypto import encrypt_secret

        self.refresh_token_encrypted = encrypt_secret(plaintext) if plaintext else None

    @property
    def granted_scopes(self) -> list[str]:
        """Return the granted OAuth scopes as a normalized list.

        GitHub reports scopes comma-separated, but this tolerates whitespace so
        callers never parse the raw string. Scopes are display-only and are
        never used to make an authorization decision (issue #57).
        """
        return [scope for scope in (self.scopes or "").replace(",", " ").split() if scope]

    def to_dict(self) -> dict:
        """Public metadata about the connection (never includes the token)."""
        return {
            "id": self.id,
            "github_user_id": self.github_user_id,
            "github_username": self.github_username,
            "scopes": self.granted_scopes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GithubAccount id={self.id} github_username={self.github_username!r}>"
