"""Custom organization roles (#605/#598).

An organization is anchored on a workspace owner (see
:mod:`app.services.organizations`). Roles are therefore stored per owner and
apply to every workspace that owner owns. Built-in rows are seeded by the
migration in ``migrations/versions``; custom rows are additive and can never
change the built-in workspace capability matrix in
:mod:`app.services.permissions`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.extensions import db

MAX_ROLE_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 255


class OrganizationRole(db.Model):
    """A built-in or custom role definition for one organization owner."""

    __tablename__ = "organization_roles"
    __table_args__ = (
        db.UniqueConstraint("owner_id", "name", name="uq_org_roles_owner_name"),
        db.Index("ix_org_roles_owner_id", "owner_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = db.Column(db.String(MAX_ROLE_NAME_LENGTH), nullable=False)
    description = db.Column(db.String(MAX_DESCRIPTION_LENGTH), nullable=True)
    capabilities_json = db.Column("capabilities", db.Text, nullable=False, default="[]")
    is_builtin = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
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

    @property
    def capabilities(self) -> list[str]:
        """Return the stored capability names as a list."""
        try:
            value = json.loads(self.capabilities_json or "[]")
        except (TypeError, ValueError):  # pragma: no cover - defensive on legacy rows
            return []
        return sorted({item for item in value if isinstance(item, str)})

    @capabilities.setter
    def capabilities(self, values: list[str]) -> None:
        self.capabilities_json = json.dumps(sorted(set(values)), sort_keys=True)

    def to_dict(self) -> dict:
        """Serialize the role for audit metadata and API responses."""
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "is_builtin": bool(self.is_builtin),
            "created_by_id": self.created_by_id,
            "updated_by_id": self.updated_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrganizationRole id={self.id} owner_id={self.owner_id} name={self.name!r}>"
