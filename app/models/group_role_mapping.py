"""SCIM group-to-role mappings and minimal group records (#593).

The organization is anchored on the workspace owner, matching the rest of the
admin console. A mapping is deliberately limited to the non-owner workspace
roles ``viewer`` and ``contributor``: a directory group can never grant the
built-in ``owner`` role, an organization ``admin`` role, or a global admin
flag. ``ScimGroup`` / ``ScimGroupMember`` hold only the directory group metadata
needed to apply those mappings to users that already exist in the database.
"""

from datetime import UTC, datetime

from app.extensions import db

MAX_EXTERNAL_ID_LENGTH = 255
MAX_DISPLAY_NAME_LENGTH = 200

#: Roles an IdP group may grant. Deliberately excludes ``owner``/``admin``.
MAPPABLE_ROLES = ("viewer", "contributor")

PROVISIONING_MANUAL = "manual"
PROVISIONING_SCIM = "scim"
PROVISIONING_SOURCES = (PROVISIONING_MANUAL, PROVISIONING_SCIM)


class GroupRoleMapping(db.Model):
    """One IdP group -> safe workspace role mapping for an organization."""

    __tablename__ = "group_role_mappings"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id",
            "group_external_id",
            name="uq_group_role_mapping_workspace_group",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_external_id = db.Column(db.String(MAX_EXTERNAL_ID_LENGTH), nullable=False)
    role = db.Column(db.String(20), nullable=False)
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

    def to_dict(self) -> dict:
        """Serialize the mapping for JSON API responses."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "group_external_id": self.group_external_id,
            "role": self.role,
            "created_by_id": self.created_by_id,
            "updated_by_id": self.updated_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<GroupRoleMapping id={self.id} group={self.group_external_id!r} "
            f"role={self.role!r}>"
        )


class ScimGroup(db.Model):
    """A minimal directory group record synchronized through SCIM."""

    __tablename__ = "scim_groups"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "external_id", name="uq_scim_group_workspace_external"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id = db.Column(db.String(MAX_EXTERNAL_ID_LENGTH), nullable=False)
    display_name = db.Column(db.String(MAX_DISPLAY_NAME_LENGTH), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    member_rows = db.relationship(
        "ScimGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="ScimGroupMember.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ScimGroup id={self.id} external_id={self.external_id!r}>"


class ScimGroupMember(db.Model):
    """One existing user in a synchronized directory group."""

    __tablename__ = "scim_group_members"
    __table_args__ = (
        db.UniqueConstraint("group_id", "user_id", name="uq_scim_group_member_group_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(
        db.Integer, db.ForeignKey("scim_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    group = db.relationship("ScimGroup", back_populates="member_rows")
    user = db.relationship("User")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ScimGroupMember group_id={self.group_id} user_id={self.user_id}>"
