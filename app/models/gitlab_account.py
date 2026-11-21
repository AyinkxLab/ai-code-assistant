"""GitLab connection metadata (#677).

The OAuth access token is never stored here: it is encrypted in
``integration_credentials`` under ``integration='gitlab'`` and
``name='access_token'``. This table only records non-sensitive account metadata
and revocation state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db


class GitlabAccount(db.Model):
    """A workspace's connected GitLab account."""

    __tablename__ = "gitlab_accounts"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "gitlab_user_id", name="uq_gitlab_accounts_workspace_user"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gitlab_user_id = db.Column(db.Integer, nullable=False)
    username = db.Column(db.String(100), nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    scopes = db.Column(db.String(500), nullable=True)
    connected_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
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

    def to_dict(self) -> dict:
        """Serialize metadata; the token is never included."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "gitlab_user_id": self.gitlab_user_id,
            "username": self.username,
            "display_name": self.display_name,
            "scopes": self.scopes,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GitlabAccount workspace_id={self.workspace_id} user={self.username!r}>"
