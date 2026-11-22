"""Verified email domains and SSO enforcement (Phase 11, #590).

An organization (workspace) proves it owns an email domain by publishing a
short-lived DNS TXT challenge. Once verified, the domain can be marked
``enforce_sso``; password sign-in for accounts on that domain is then refused
until they authenticate through the organization's SSO flow.

The challenge token is only an ownership proof, not an authentication secret:
someone who can publish the TXT record already controls the domain. It is
still bounded by :data:`~app.services.domain_verification.CHALLENGE_TTL` so an
abandoned challenge cannot be completed indefinitely.
"""

from datetime import UTC, datetime

from app.extensions import db


class WorkspaceDomain(db.Model):
    """One email domain claimed by a workspace."""

    __tablename__ = "workspace_domains"
    __table_args__ = (db.UniqueConstraint("domain", name="uq_workspace_domains_domain"),)

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain = db.Column(db.String(253), nullable=False, index=True)
    verification_token = db.Column(db.String(128), nullable=False)
    challenge_expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    enforce_sso = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def to_dict(self) -> dict:
        """Serialize the domain without exposing internal implementation detail."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "domain": self.domain,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "challenge_expires_at": (
                self.challenge_expires_at.isoformat() if self.challenge_expires_at else None
            ),
            "enforce_sso": bool(self.enforce_sso),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkspaceDomain domain={self.domain!r} workspace_id={self.workspace_id}>"
