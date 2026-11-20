"""Plugin database models.

Stores plugin metadata, installations, and configuration per workspace.
"""

from datetime import UTC, datetime

from app.extensions import db


class Plugin(db.Model):
    """Installed plugin metadata (global)."""

    __tablename__ = "plugins"

    id = db.Column(db.String(64), primary_key=True)  # e.g., "stellar-tools"
    name = db.Column(db.String(256), nullable=False)
    version = db.Column(db.String(32), nullable=False)
    description = db.Column(db.Text)
    author = db.Column(db.String(256))
    entry_point = db.Column(db.String(512), nullable=False)
    capabilities = db.Column(db.JSON, default=[])  # List of capability strings
    permissions = db.Column(db.JSON, default=[])
    dependencies = db.Column(db.JSON, default=[])
    compatibility = db.Column(db.String(256), nullable=True)
    configuration = db.Column(db.JSON, default={})
    trust_state = db.Column(db.String(16), nullable=False, default="Unverified")
    trust_publisher = db.Column(db.String(256), nullable=True)

    enabled = db.Column(db.Boolean, default=True)
    installed_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    installations = db.relationship(
        "PluginInstallation",
        back_populates="plugin",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entry_point": self.entry_point,
            "capabilities": self.capabilities or [],
            "permissions": self.permissions or [],
            "dependencies": self.dependencies or [],
            "compatibility": self.compatibility,
            "configuration": self.configuration or {},
            "trust_state": self.trust_state,
            "trust_publisher": self.trust_publisher,
            "enabled": self.enabled,
            "installed_at": self.installed_at.isoformat() if self.installed_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        """String representation."""
        return f"<Plugin {self.id} v{self.version}>"


class PluginInstallation(db.Model):
    """Plugin installation in a specific workspace.

    Tracks plugin-specific installation, configuration, and granted capabilities
    within a workspace.
    """

    __tablename__ = "plugin_installations"

    id = db.Column(db.Integer, primary_key=True)
    plugin_id = db.Column(db.String(64), db.ForeignKey("plugins.id"), nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True)

    enabled = db.Column(db.Boolean, default=True)
    granted_capabilities = db.Column(db.JSON, default=[])  # List of capability strings
    config = db.Column(db.JSON, default={})  # Workspace-specific plugin configuration

    installed_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    installed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Relationships
    plugin = db.relationship("Plugin", back_populates="installations", lazy="select")
    installed_by = db.relationship("User", foreign_keys=[installed_by_id], lazy="select")
    workspace = db.relationship("Workspace", foreign_keys=[workspace_id], lazy="select")

    __table_args__ = (
        db.UniqueConstraint("plugin_id", "workspace_id", name="uq_plugin_installation"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "plugin_id": self.plugin_id,
            "workspace_id": self.workspace_id,
            "enabled": self.enabled,
            "granted_capabilities": self.granted_capabilities or [],
            "config": self.config or {},
            "installed_at": self.installed_at.isoformat() if self.installed_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "installed_by_id": self.installed_by_id,
        }

    def __repr__(self) -> str:
        """String representation."""
        return f"<PluginInstallation plugin={self.plugin_id} workspace={self.workspace_id}>"


class CapabilityGrant(db.Model):
    """Track capabilities granted to a plugin within a workspace.

    A capability grant represents the intersection of:
    - Plugin ID
    - Workspace ID
    - Granted capability
    - User who granted the capability (for audit)

    A plugin may only act on a workspace after an explicit grant row exists,
    so capabilities are never granted automatically.
    """

    __tablename__ = "plugin_capability_grants"

    id = db.Column(db.Integer, primary_key=True)
    plugin_id = db.Column(db.String(64), db.ForeignKey("plugins.id"), nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True)
    capability = db.Column(db.String(64), nullable=False)

    granted_at = db.Column(db.DateTime, default=db.func.now(), nullable=False)
    granted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Relationships
    granted_by = db.relationship("User", foreign_keys=[granted_by_id])

    __table_args__ = (
        db.UniqueConstraint(
            "plugin_id",
            "workspace_id",
            "capability",
            name="uq_plugin_capability_grant",
        ),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "plugin_id": self.plugin_id,
            "workspace_id": self.workspace_id,
            "capability": self.capability,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
            "granted_by_id": self.granted_by_id,
        }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<CapabilityGrant plugin_id={self.plugin_id} workspace_id={self.workspace_id} "
            f"capability={self.capability}>"
        )


class PluginLockEntry(db.Model):
    """A resolved plugin pinned for reproducible installs (#394).

    A lock row records exactly what a workspace resolved: the plugin id, the
    pinned version, where it came from, and the sha256 integrity hash of the
    artifact. Rows are scoped to both the workspace and its owner, so one
    workspace can never read or overwrite another's lockfile, and a lock entry
    never grants a capability — grants stay explicit and live in
    ``CapabilityGrant``.

    The row is intentionally free of volatile fields in its lockfile form:
    ``resolved_at`` is for operators, while
    :meth:`~app.services.plugin_lockfile.build_lockfile` serializes only the
    stable fields (and accepts these rows directly), keeping lockfiles
    byte-stable across resolutions.
    """

    __tablename__ = "plugin_lock_entries"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plugin_id = db.Column(db.String(64), nullable=False, index=True)
    version = db.Column(db.String(32), nullable=False)
    source = db.Column(db.String(512), nullable=False)
    integrity = db.Column(db.String(128), nullable=False)
    dependencies = db.Column(db.JSON, default=[], nullable=False)

    # Previous resolved artifact, retained so a rollback can restore it even
    # after the catalog stops listing that version (#363).
    previous_version = db.Column(db.String(32), nullable=True)
    previous_source = db.Column(db.String(512), nullable=True)
    previous_integrity = db.Column(db.String(128), nullable=True)
    previous_manifest = db.Column(db.JSON, nullable=True)
    manifest = db.Column(db.JSON, nullable=True)

    resolved_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    workspace = db.relationship("Workspace", foreign_keys=[workspace_id], lazy="select")
    owner = db.relationship("User", foreign_keys=[owner_id], lazy="select")

    __table_args__ = (
        db.UniqueConstraint("workspace_id", "plugin_id", name="uq_plugin_lock_entry"),
    )

    def to_lockfile_entry(self):
        """Return the canonical :class:`LockfileEntry` for this row.

        Imported lazily so the model module does not depend on the service
        layer at import time.
        """
        from app.services.plugin_lockfile import LockfileEntry, normalize_integrity

        return LockfileEntry(
            plugin_id=self.plugin_id,
            version=self.version,
            source=self.source,
            integrity=normalize_integrity(self.integrity),
            dependencies=tuple(self.dependencies or ()),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary representation.

        The result includes audit fields (``id``, ``workspace_id``,
        ``owner_id``, ``resolved_at``) for API reads. Use
        :meth:`to_lockfile_entry` (or pass the row to ``build_lockfile``) when
        producing the canonical, byte-stable lockfile.
        """
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "source": self.source,
            "integrity": self.integrity,
            "dependencies": self.dependencies or [],
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<PluginLockEntry plugin={self.plugin_id} version={self.version} "
            f"workspace={self.workspace_id}>"
        )
