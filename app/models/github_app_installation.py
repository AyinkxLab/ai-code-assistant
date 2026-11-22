"""GitHub App installation model (#676).

A GitHub App installation is stored per workspace so its installation id and
repository selection can be updated and revoked independently of the existing
OAuth account connection. Any installation access token is encrypted with the
same Fernet helper used for OAuth tokens; only the ciphertext is persisted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.extensions import db

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_REVOKED = "revoked"
STATUSES = (STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_REVOKED)

REPOSITORY_SELECTION_ALL = "all"
REPOSITORY_SELECTION_SELECTED = "selected"
REPOSITORY_SELECTIONS = (REPOSITORY_SELECTION_ALL, REPOSITORY_SELECTION_SELECTED)


class GitHubAppInstallation(db.Model):
    """A workspace-scoped GitHub App installation."""

    __tablename__ = "github_app_installations"
    __table_args__ = (
        db.UniqueConstraint("installation_id", name="uq_github_app_installations_installation"),
        db.Index(
            "ix_github_app_installations_workspace_status",
            "workspace_id",
            "status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    installation_id = db.Column(db.BigInteger, nullable=False)
    account_id = db.Column(db.BigInteger, nullable=True)
    account_login = db.Column(db.String(200), nullable=False)
    account_type = db.Column(db.String(50), nullable=False, default="User")
    repository_selection = db.Column(
        db.String(20),
        nullable=False,
        default=REPOSITORY_SELECTION_SELECTED,
    )
    #: JSON array of selected repository ids. Empty for ``repository_selection``
    #: of ``all``; the selection flag remains authoritative.
    repository_ids_json = db.Column(db.Text, nullable=False, default="[]")
    #: Encrypted installation access token, when one has been issued.
    access_token_encrypted = db.Column(db.Text, nullable=True)
    token_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE)
    suspended_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def set_access_token(self, plaintext: str | None) -> None:
        """Encrypt and store an installation token, or clear it when absent."""
        from app.services.crypto import encrypt_secret

        self.access_token_encrypted = encrypt_secret(plaintext) if plaintext else None

    def access_token(self) -> str | None:
        """Decrypt the installation token in memory, or return ``None``."""
        from app.services.crypto import decrypt_secret

        if not self.access_token_encrypted:
            return None
        return decrypt_secret(self.access_token_encrypted)

    def set_repository_ids(self, values) -> None:
        """Store a normalized, de-duplicated list of positive repository ids."""
        repository_ids: list[int] = []
        for value in values or []:
            if isinstance(value, bool):
                raise ValueError("repository ids must be integers")
            try:
                repository_id = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("repository ids must be integers") from exc
            if repository_id <= 0:
                raise ValueError("repository ids must be positive integers")
            if repository_id not in repository_ids:
                repository_ids.append(repository_id)
        self.repository_ids_json = json.dumps(repository_ids, separators=(",", ":"))

    def repository_ids(self) -> list[int]:
        """Return the selected repository ids as integers."""
        try:
            values = json.loads(self.repository_ids_json or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, int) and not isinstance(value, bool)]

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def to_dict(self) -> dict:
        """Serialize metadata only; the token and ciphertext are never included."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "installation_id": self.installation_id,
            "account_id": self.account_id,
            "account_login": self.account_login,
            "account_type": self.account_type,
            "repository_selection": self.repository_selection,
            "repository_ids": self.repository_ids(),
            "has_access_token": bool(self.access_token_encrypted),
            "status": self.status,
            "suspended_at": self.suspended_at.isoformat() if self.suspended_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<GitHubAppInstallation id={self.id} installation_id={self.installation_id} "
            f"workspace_id={self.workspace_id} status={self.status!r}>"
        )
