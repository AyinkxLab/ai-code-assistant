"""Durable links from security findings to external tickets (#630).

One row per ``(workspace_id, finding_id)`` is the duplicate guard: retrying an
acknowledgement returns the stored ticket instead of creating another issue.
``(provider, external_key)`` is also unique so the same external issue cannot
be recorded twice.
"""

from datetime import UTC, datetime

from app.extensions import db


class SecurityTicketLink(db.Model):
    """A high-risk finding exported to Jira or Linear when acknowledged."""

    __tablename__ = "security_ticket_links"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id",
            "finding_id",
            name="uq_security_ticket_links_workspace_finding",
        ),
        db.UniqueConstraint(
            "provider",
            "external_key",
            name="uq_security_ticket_links_provider_external",
        ),
        db.Index("ix_security_ticket_links_workspace_created", "workspace_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    finding_id = db.Column(
        db.Integer,
        db.ForeignKey("review_findings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = db.Column(db.String(20), nullable=False)
    external_key = db.Column(db.String(100), nullable=False, index=True)
    external_url = db.Column(db.String(500), nullable=True)
    severity = db.Column(db.String(20), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "finding_id": self.finding_id,
            "provider": self.provider,
            "external_key": self.external_key,
            "external_url": self.external_url,
            "severity": self.severity,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<SecurityTicketLink id={self.id} provider={self.provider!r} "
            f"external_key={self.external_key!r}>"
        )
