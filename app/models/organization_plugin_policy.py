"""Organization plugin governance policy and approvals (#634).

An organization is anchored on the workspace owner, matching
:mod:`app.services.organizations`: every workspace owned by the same user is
part of the same organization. The policy is one row per organization owner;
``mode`` is ``allow``, ``block``, or ``require_approval``. Approvals are stored
per organization and plugin id so a plugin approved once stays installable.
"""

from datetime import UTC, datetime

from app.extensions import db

MODE_ALLOW = "allow"
MODE_BLOCK = "block"
MODE_REQUIRE_APPROVAL = "require_approval"
MODES = (MODE_ALLOW, MODE_BLOCK, MODE_REQUIRE_APPROVAL)

#: Hard cap for a plugin id in an approval row (matches the plugin id column).
MAX_PLUGIN_ID_LENGTH = 64


class OrganizationPluginPolicy(db.Model):
    """The plugin install policy for one organization."""

    __tablename__ = "organization_plugin_policies"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    mode = db.Column(db.String(32), nullable=False, default=MODE_ALLOW)
    updated_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
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
        """Serialize the policy for API output."""
        return {
            "owner_id": self.owner_id,
            "mode": self.mode,
            "updated_by_id": self.updated_by_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrganizationPluginPolicy owner_id={self.owner_id} mode={self.mode!r}>"


class PluginApproval(db.Model):
    """An explicit organization approval for one plugin id."""

    __tablename__ = "plugin_approvals"
    __table_args__ = (
        db.UniqueConstraint("owner_id", "plugin_id", name="uq_plugin_approval_owner_plugin"),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plugin_id = db.Column(db.String(MAX_PLUGIN_ID_LENGTH), nullable=False)
    approved_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict:
        """Serialize the approval for API output."""
        return {
            "id": self.id,
            "plugin_id": self.plugin_id,
            "approved_by_id": self.approved_by_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PluginApproval owner_id={self.owner_id} plugin_id={self.plugin_id!r}>"
