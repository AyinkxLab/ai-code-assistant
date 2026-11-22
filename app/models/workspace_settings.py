"""Per-workspace collaboration settings.

Holds policy switches owners use to control collaboration. Kept as a separate
one-to-one table so the base ``Workspace`` row stays lean. Defaults preserve
the pre-Phase-7 behavior: invitations are enabled and new members default to
the viewer role.
"""

from datetime import UTC, datetime

from app.extensions import db
from app.models.workspace_member import ROLE_VIEWER

DEFAULT_MEMBER_ROLES = ("viewer", "contributor")


class WorkspaceSettings(db.Model):
    """One-to-one collaboration settings for a workspace."""

    __tablename__ = "workspace_settings"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    invitations_enabled = db.Column(db.Boolean, nullable=False, default=True)
    default_member_role = db.Column(db.String(20), nullable=False, default=ROLE_VIEWER)
    #: Require enrolled MFA for members before they can sign in (#618).
    require_mfa = db.Column(db.Boolean, nullable=False, default=False)
    #: When true, ``require_mfa`` applies only to owners/contributors.
    mfa_privileged_only = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    workspace = db.relationship("Workspace", back_populates="settings")

    def to_dict(self) -> dict:
        """Serialize the settings for JSON API responses."""
        return {
            "workspace_id": self.workspace_id,
            "invitations_enabled": self.invitations_enabled,
            "default_member_role": self.default_member_role,
            "require_mfa": self.require_mfa,
            "mfa_privileged_only": self.mfa_privileged_only,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkspaceSettings workspace_id={self.workspace_id}>"


def validate_member_role(role: str | None) -> str | None:
    """Return a normalized default member role, or ``None`` when invalid."""
    role = (role or "").strip().lower()
    if role in DEFAULT_MEMBER_ROLES:
        return role
    return None
