"""Per-integration health metadata (#684).

Health rows hold only non-secret operational facts: when an integration last
succeeded, when it last failed, a short sanitized error label, and the last HTTP
status code. Webhook URLs, signing secrets, and inbound credentials never appear
here; the error text is redacted by the service before it is stored.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

STATUS_UNKNOWN = "unknown"
STATUS_HEALTHY = "healthy"
STATUS_ERROR = "error"
STATUSES = (STATUS_UNKNOWN, STATUS_HEALTHY, STATUS_ERROR)

INTEGRATIONS = ("slack", "teams", "email")


class IntegrationHealth(db.Model):
    """The latest health snapshot for one workspace integration."""

    __tablename__ = "integration_health"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id",
            "integration",
            name="uq_integration_health_workspace_integration",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    integration = db.Column(db.String(50), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_UNKNOWN)
    last_success_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_error_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_error = db.Column(db.String(500), nullable=True)
    last_error_code = db.Column(db.String(50), nullable=True)
    last_status_code = db.Column(db.Integer, nullable=True)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)
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
        """Return non-secret health metadata for API responses."""
        return {
            "integration": self.integration,
            "status": self.status,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_error": self.last_error,
            "last_error_code": self.last_error_code,
            "last_status_code": self.last_status_code,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<IntegrationHealth workspace_id={self.workspace_id} "
            f"integration={self.integration!r} status={self.status!r}>"
        )
