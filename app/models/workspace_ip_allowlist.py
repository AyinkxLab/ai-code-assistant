"""Per-workspace IP allowlist (#620).

One row per workspace. ``cidrs`` is a JSON array of canonical CIDR strings
(IPv4 or IPv6). The allowlist is disabled by default, so existing deployments
keep working until an owner configures it. When enabled with no entries the
request guard fails closed (denies every request for that workspace).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.extensions import db


class WorkspaceIPAllowlist(db.Model):
    """The organization-wide IP allowlist for one workspace."""

    __tablename__ = "workspace_ip_allowlists"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    #: JSON array of canonical CIDR strings. Empty means "nothing is allowed".
    cidrs = db.Column(db.Text, nullable=False, default="[]")
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

    def parsed_cidrs(self) -> list[str]:
        """Return the stored CIDR list, or an empty list when malformed."""
        try:
            values = json.loads(self.cidrs or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, str)]

    def to_dict(self) -> dict:
        """Serialize the allowlist for JSON API responses."""
        return {
            "workspace_id": self.workspace_id,
            "enabled": bool(self.enabled),
            "cidrs": self.parsed_cidrs(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<WorkspaceIPAllowlist workspace_id={self.workspace_id} " f"enabled={self.enabled!r}>"
        )
