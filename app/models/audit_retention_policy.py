"""Per-workspace audit retention policy and legal hold (#608).

One row per workspace. The setting is validated in
:mod:`app.services.audit_retention`; a workspace with no row uses the default
retention and no legal hold. A legal hold is a hard stop: the purge job never
deletes a row while it is active, regardless of age.
"""

from datetime import UTC, datetime

from app.extensions import db


class AuditRetentionPolicy(db.Model):
    """Retention window and legal-hold flag for one workspace's audit log."""

    __tablename__ = "audit_retention_policies"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    retention_days = db.Column(db.Integer, nullable=False, default=365)
    legal_hold = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    workspace = db.relationship("Workspace")

    def to_dict(self) -> dict:
        """Serialize the policy for API/CLI output."""
        return {
            "workspace_id": self.workspace_id,
            "retention_days": self.retention_days,
            "legal_hold": bool(self.legal_hold),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<AuditRetentionPolicy workspace_id={self.workspace_id} "
            f"retention_days={self.retention_days} legal_hold={self.legal_hold}>"
        )
