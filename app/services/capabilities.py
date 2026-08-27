"""Plugin capability model and management.

Capabilities define what actions plugins are allowed to perform within
the application. They integrate with workspace roles and permissions.
"""

from enum import Enum

from app.extensions import db
from app.models.plugin import CapabilityGrant


class Capability(Enum):
    """Plugin capabilities (permissions).

    Capabilities must be explicitly granted to plugins. They are mapped
    to workspace member roles to control access.
    """

    # Project capabilities
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    PROJECT_DELETE = "project:delete"

    # Workspace capabilities
    WORKSPACE_READ = "workspace:read"
    WORKSPACE_WRITE = "workspace:write"

    # GitHub capabilities
    GITHUB_READ = "github:read"
    GITHUB_WRITE = "github:write"

    # AI capabilities
    AI_ACCESS = "ai:access"
    AI_ANALYSIS = "ai:analysis"

    # Notification capabilities
    NOTIFICATION_CREATE = "notification:create"

    # Stellar capabilities
    STELLAR_READ = "stellar:read"
    STELLAR_WRITE = "stellar:write"
    STELLAR_ANALYSIS = "stellar:analysis"

    # Code review capabilities
    REVIEW_READ = "review:read"
    REVIEW_CREATE = "review:create"

    @classmethod
    def from_string(cls, value: str) -> "Capability | None":
        """Get capability from string value.

        Args:
            value: String value (e.g., "project:read")

        Returns:
            Capability enum or None if not found
        """
        for cap in cls:
            if cap.value == value:
                return cap
        return None

    @classmethod
    def all_values(cls) -> list[str]:
        """Get all capability string values.

        Returns:
            List of capability strings
        """
        return [cap.value for cap in cls]


# Mapping of workspace member roles to capabilities.
# This defines what capabilities are granted based on role.
ROLE_CAPABILITY_MAPPING = {
    "viewer": [
        Capability.PROJECT_READ,
        Capability.WORKSPACE_READ,
        Capability.AI_ACCESS,
        Capability.STELLAR_READ,
        Capability.REVIEW_READ,
    ],
    "contributor": [
        Capability.PROJECT_READ,
        Capability.PROJECT_WRITE,
        Capability.WORKSPACE_READ,
        Capability.AI_ACCESS,
        Capability.AI_ANALYSIS,
        Capability.GITHUB_READ,
        Capability.STELLAR_READ,
        Capability.STELLAR_ANALYSIS,
        Capability.REVIEW_READ,
        Capability.REVIEW_CREATE,
    ],
    "admin": [
        Capability.PROJECT_READ,
        Capability.PROJECT_WRITE,
        Capability.PROJECT_DELETE,
        Capability.WORKSPACE_READ,
        Capability.WORKSPACE_WRITE,
        Capability.AI_ACCESS,
        Capability.AI_ANALYSIS,
        Capability.GITHUB_READ,
        Capability.GITHUB_WRITE,
        Capability.NOTIFICATION_CREATE,
        Capability.STELLAR_READ,
        Capability.STELLAR_WRITE,
        Capability.STELLAR_ANALYSIS,
        Capability.REVIEW_READ,
        Capability.REVIEW_CREATE,
    ],
    "owner": [
        Capability.PROJECT_READ,
        Capability.PROJECT_WRITE,
        Capability.PROJECT_DELETE,
        Capability.WORKSPACE_READ,
        Capability.WORKSPACE_WRITE,
        Capability.AI_ACCESS,
        Capability.AI_ANALYSIS,
        Capability.GITHUB_READ,
        Capability.GITHUB_WRITE,
        Capability.NOTIFICATION_CREATE,
        Capability.STELLAR_READ,
        Capability.STELLAR_WRITE,
        Capability.STELLAR_ANALYSIS,
        Capability.REVIEW_READ,
        Capability.REVIEW_CREATE,
    ],
}


class CapabilityStore:
    """Manage plugin capability grants.

    Capabilities define what actions plugins can perform. This store manages
    granting and revoking capabilities to plugins within specific workspaces.
    """

    @staticmethod
    def grant(
        plugin_id: str,
        workspace_id: int,
        capability: str,
        granted_by_id: int | None = None,
    ) -> CapabilityGrant:
        """Grant a capability to a plugin in a workspace.

        Args:
            plugin_id: Plugin identifier
            workspace_id: Workspace identifier
            capability: Capability string (e.g., "project:read")
            granted_by_id: User ID who is granting the capability

        Returns:
            CapabilityGrant record

        Raises:
            ValueError: If capability is invalid
        """
        # Validate capability
        if Capability.from_string(capability) is None:
            raise ValueError(f"Invalid capability: {capability}")

        # Check if grant already exists
        existing = CapabilityGrant.query.filter_by(
            plugin_id=plugin_id,
            workspace_id=workspace_id,
            capability=capability,
        ).first()

        if existing:
            return existing

        grant = CapabilityGrant(
            plugin_id=plugin_id,
            workspace_id=workspace_id,
            capability=capability,
            granted_by_id=granted_by_id,
        )
        db.session.add(grant)
        db.session.commit()
        return grant

    @staticmethod
    def revoke(
        plugin_id: str,
        workspace_id: int,
        capability: str,
    ) -> bool:
        """Revoke a capability from a plugin in a workspace.

        Args:
            plugin_id: Plugin identifier
            workspace_id: Workspace identifier
            capability: Capability string

        Returns:
            True if revoked, False if grant did not exist
        """
        grant = CapabilityGrant.query.filter_by(
            plugin_id=plugin_id,
            workspace_id=workspace_id,
            capability=capability,
        ).first()

        if not grant:
            return False

        db.session.delete(grant)
        db.session.commit()
        return True

    @staticmethod
    def has_capability(
        plugin_id: str,
        workspace_id: int,
        capability: str,
    ) -> bool:
        """Check if plugin has capability in workspace.

        Args:
            plugin_id: Plugin identifier
            workspace_id: Workspace identifier
            capability: Capability string

        Returns:
            True if plugin has capability
        """
        grant = CapabilityGrant.query.filter_by(
            plugin_id=plugin_id,
            workspace_id=workspace_id,
            capability=capability,
        ).first()
        return grant is not None

    @staticmethod
    def list_capabilities(plugin_id: str, workspace_id: int) -> list[str]:
        """List all capabilities granted to plugin in workspace.

        Args:
            plugin_id: Plugin identifier
            workspace_id: Workspace identifier

        Returns:
            List of capability strings
        """
        grants = CapabilityGrant.query.filter_by(
            plugin_id=plugin_id,
            workspace_id=workspace_id,
        ).all()
        return [grant.capability for grant in grants]

    @staticmethod
    def list_granted_plugins(workspace_id: int, capability: str | None = None) -> list[str]:
        """List all plugins with capabilities in a workspace.

        Args:
            workspace_id: Workspace identifier
            capability: Filter by specific capability (optional)

        Returns:
            List of plugin IDs
        """
        query = CapabilityGrant.query.filter_by(workspace_id=workspace_id)
        if capability:
            query = query.filter_by(capability=capability)
        grants = query.all()
        return list({grant.plugin_id for grant in grants})

    @staticmethod
    def revoke_all(plugin_id: str, workspace_id: int) -> int:
        """Revoke all capabilities from a plugin in a workspace.

        Args:
            plugin_id: Plugin identifier
            workspace_id: Workspace identifier

        Returns:
            Number of capabilities revoked
        """
        grants = CapabilityGrant.query.filter_by(
            plugin_id=plugin_id,
            workspace_id=workspace_id,
        ).all()
        count = len(grants)
        for grant in grants:
            db.session.delete(grant)
        db.session.commit()
        return count

    @staticmethod
    def get_role_capabilities(role: str) -> list[str]:
        """Get capabilities for a workspace member role.

        Args:
            role: Workspace member role (e.g., "viewer", "contributor", "admin")

        Returns:
            List of capability strings available for the role
        """
        capabilities = ROLE_CAPABILITY_MAPPING.get(role, [])
        return [cap.value for cap in capabilities]


def validate_plugin_capability(
    plugin_id: str,
    workspace_id: int,
    capability: str,
) -> tuple[bool, str]:
    """Validate that a plugin has been granted a capability.

    Args:
        plugin_id: Plugin identifier
        workspace_id: Workspace identifier
        capability: Capability to check

    Returns:
        (is_valid, error_message or empty string)
    """
    # Validate capability string format
    if Capability.from_string(capability) is None:
        return (False, f"Invalid capability: {capability}")

    # Check if grant exists
    if not CapabilityStore.has_capability(plugin_id, workspace_id, capability):
        return (
            False,
            f"Plugin {plugin_id} does not have capability {capability} "
            f"in workspace {workspace_id}",
        )

    return (True, "")
