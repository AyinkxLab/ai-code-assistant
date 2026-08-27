"""Tests for plugin capability management."""

import pytest

from app.services.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityStore,
    ROLE_CAPABILITY_MAPPING,
    validate_plugin_capability,
)


class TestCapability:
    """Test Capability enum."""

    def test_capability_enum_values(self):
        """Test that all capabilities have values."""
        for cap in Capability:
            assert cap.value is not None
            assert isinstance(cap.value, str)
            assert ":" in cap.value  # All values should be "domain:action"

    def test_capability_from_string(self):
        """Test getting capability from string."""
        cap = Capability.from_string("project:read")
        assert cap == Capability.PROJECT_READ

    def test_capability_from_string_invalid(self):
        """Test getting invalid capability from string."""
        cap = Capability.from_string("invalid:capability")
        assert cap is None

    def test_capability_all_values(self):
        """Test getting all capability values."""
        values = Capability.all_values()
        assert len(values) == 15  # We have 15 capabilities defined
        assert "project:read" in values
        assert "stellar:read" in values
        assert all(":" in v for v in values)

    def test_capability_enum_count(self):
        """Test that we have expected number of capabilities."""
        capabilities = [
            "PROJECT_READ",
            "PROJECT_WRITE",
            "PROJECT_DELETE",
            "WORKSPACE_READ",
            "WORKSPACE_WRITE",
            "GITHUB_READ",
            "GITHUB_WRITE",
            "AI_ACCESS",
            "AI_ANALYSIS",
            "NOTIFICATION_CREATE",
            "STELLAR_READ",
            "STELLAR_WRITE",
            "STELLAR_ANALYSIS",
            "REVIEW_READ",
            "REVIEW_CREATE",
        ]
        for cap_name in capabilities:
            assert hasattr(Capability, cap_name)


class TestRoleCapabilityMapping:
    """Test role to capability mapping."""

    def test_all_roles_defined(self):
        """Test that all roles have capability mappings."""
        roles = ["viewer", "contributor", "admin", "owner"]
        for role in roles:
            assert role in ROLE_CAPABILITY_MAPPING
            assert len(ROLE_CAPABILITY_MAPPING[role]) > 0

    def test_viewer_capabilities(self):
        """Test viewer role has expected capabilities."""
        viewer_caps = ROLE_CAPABILITY_MAPPING["viewer"]
        assert Capability.PROJECT_READ in viewer_caps
        assert Capability.WORKSPACE_READ in viewer_caps
        assert Capability.AI_ACCESS in viewer_caps
        assert Capability.STELLAR_READ in viewer_caps
        # Viewer should not have write capabilities
        assert Capability.PROJECT_WRITE not in viewer_caps
        assert Capability.WORKSPACE_WRITE not in viewer_caps

    def test_contributor_capabilities(self):
        """Test contributor role has expected capabilities."""
        contrib_caps = ROLE_CAPABILITY_MAPPING["contributor"]
        assert Capability.PROJECT_READ in contrib_caps
        assert Capability.PROJECT_WRITE in contrib_caps
        assert Capability.AI_ANALYSIS in contrib_caps
        assert Capability.STELLAR_ANALYSIS in contrib_caps
        assert Capability.REVIEW_CREATE in contrib_caps
        # Contributor should not have delete/admin capabilities
        assert Capability.PROJECT_DELETE not in contrib_caps
        assert Capability.WORKSPACE_WRITE not in contrib_caps

    def test_admin_capabilities(self):
        """Test admin role has write capabilities."""
        admin_caps = ROLE_CAPABILITY_MAPPING["admin"]
        assert Capability.PROJECT_DELETE in admin_caps
        assert Capability.WORKSPACE_WRITE in admin_caps
        assert Capability.GITHUB_WRITE in admin_caps
        assert Capability.STELLAR_WRITE in admin_caps

    def test_owner_capabilities(self):
        """Test owner role has all capabilities."""
        owner_caps = ROLE_CAPABILITY_MAPPING["owner"]
        for cap in Capability:
            assert cap in owner_caps, f"Owner should have {cap.name}"

    def test_role_hierarchy(self):
        """Test that role permissions are hierarchical."""
        viewer = set(ROLE_CAPABILITY_MAPPING["viewer"])
        contributor = set(ROLE_CAPABILITY_MAPPING["contributor"])
        admin = set(ROLE_CAPABILITY_MAPPING["admin"])
        owner = set(ROLE_CAPABILITY_MAPPING["owner"])

        # Each higher role should have at least the permissions of lower roles
        assert viewer.issubset(contributor), "Contributor should have viewer permissions"
        assert contributor.issubset(admin), "Admin should have contributor permissions"
        assert admin.issubset(owner), "Owner should have admin permissions"


class TestCapabilityStore:
    """Test capability store operations (requires database)."""

    def test_grant_capability(self, app):
        """Test granting a capability."""
        with app.app_context():
            grant = CapabilityStore.grant("test-plugin", 1, "project:read")
            assert grant.plugin_id == "test-plugin"
            assert grant.workspace_id == 1
            assert grant.capability == "project:read"

    def test_grant_duplicate_capability(self, app):
        """Test granting same capability twice returns existing grant."""
        with app.app_context():
            grant1 = CapabilityStore.grant("test-plugin", 1, "project:read")
            grant2 = CapabilityStore.grant("test-plugin", 1, "project:read")
            assert grant1.id == grant2.id

    def test_grant_invalid_capability(self, app):
        """Test granting invalid capability raises error."""
        with app.app_context():
            with pytest.raises(ValueError) as exc_info:
                CapabilityStore.grant("test-plugin", 1, "invalid:capability")
            assert "Invalid capability" in str(exc_info.value)

    def test_revoke_capability(self, app):
        """Test revoking a capability."""
        with app.app_context():
            CapabilityStore.grant("test-plugin", 1, "project:read")
            result = CapabilityStore.revoke("test-plugin", 1, "project:read")
            assert result is True
            assert not CapabilityStore.has_capability("test-plugin", 1, "project:read")

    def test_revoke_nonexistent_capability(self, app):
        """Test revoking non-existent capability returns False."""
        with app.app_context():
            result = CapabilityStore.revoke("test-plugin", 1, "project:read")
            assert result is False

    def test_has_capability(self, app):
        """Test checking if plugin has capability."""
        with app.app_context():
            CapabilityStore.grant("test-plugin", 1, "project:read")
            assert CapabilityStore.has_capability("test-plugin", 1, "project:read")
            assert not CapabilityStore.has_capability("test-plugin", 1, "project:write")

    def test_list_capabilities(self, app):
        """Test listing all capabilities for plugin in workspace."""
        with app.app_context():
            CapabilityStore.grant("test-plugin", 1, "project:read")
            CapabilityStore.grant("test-plugin", 1, "project:write")
            CapabilityStore.grant("test-plugin", 1, "stellar:read")

            caps = CapabilityStore.list_capabilities("test-plugin", 1)
            assert len(caps) == 3
            assert "project:read" in caps
            assert "project:write" in caps
            assert "stellar:read" in caps

    def test_list_capabilities_empty(self, app):
        """Test listing capabilities for plugin with none granted."""
        with app.app_context():
            caps = CapabilityStore.list_capabilities("nonexistent", 1)
            assert caps == []

    def test_list_granted_plugins(self, app):
        """Test listing all plugins granted a capability."""
        with app.app_context():
            CapabilityStore.grant("plugin-1", 1, "project:read")
            CapabilityStore.grant("plugin-2", 1, "project:read")
            CapabilityStore.grant("plugin-3", 1, "project:write")

            plugins = CapabilityStore.list_granted_plugins(1, "project:read")
            assert len(plugins) == 2
            assert "plugin-1" in plugins
            assert "plugin-2" in plugins
            assert "plugin-3" not in plugins

    def test_revoke_all_capabilities(self, app):
        """Test revoking all capabilities from a plugin."""
        with app.app_context():
            CapabilityStore.grant("test-plugin", 1, "project:read")
            CapabilityStore.grant("test-plugin", 1, "project:write")
            CapabilityStore.grant("test-plugin", 1, "stellar:read")

            count = CapabilityStore.revoke_all("test-plugin", 1)
            assert count == 3

            caps = CapabilityStore.list_capabilities("test-plugin", 1)
            assert len(caps) == 0

    def test_get_role_capabilities(self, app):
        """Test getting capabilities for a role."""
        with app.app_context():
            viewer_caps = CapabilityStore.get_role_capabilities("viewer")
            assert len(viewer_caps) > 0
            assert "project:read" in viewer_caps
            assert "project:write" not in viewer_caps

            admin_caps = CapabilityStore.get_role_capabilities("admin")
            assert len(admin_caps) > len(viewer_caps)
            assert "project:write" in admin_caps
            assert "project:delete" in admin_caps

    def test_get_role_capabilities_invalid_role(self, app):
        """Test getting capabilities for invalid role."""
        with app.app_context():
            caps = CapabilityStore.get_role_capabilities("invalid_role")
            assert caps == []


class TestCapabilityGrant:
    """Test CapabilityGrant model."""

    def test_capability_grant_to_dict(self, app):
        """Test converting grant to dictionary."""
        with app.app_context():
            grant = CapabilityStore.grant("test-plugin", 1, "project:read")
            result = grant.to_dict()
            assert result["plugin_id"] == "test-plugin"
            assert result["workspace_id"] == 1
            assert result["capability"] == "project:read"
            assert "granted_at" in result

    def test_capability_grant_repr(self, app):
        """Test grant string representation."""
        with app.app_context():
            grant = CapabilityStore.grant("test-plugin", 1, "project:read")
            repr_str = repr(grant)
            assert "test-plugin" in repr_str
            assert "project:read" in repr_str


class TestValidatePluginCapability:
    """Test capability validation function."""

    def test_validate_valid_capability(self, app):
        """Test validating a valid capability grant."""
        with app.app_context():
            CapabilityStore.grant("test-plugin", 1, "project:read")
            is_valid, error = validate_plugin_capability("test-plugin", 1, "project:read")
            assert is_valid is True
            assert error == ""

    def test_validate_invalid_capability_format(self, app):
        """Test validating invalid capability format."""
        with app.app_context():
            is_valid, error = validate_plugin_capability("test-plugin", 1, "invalid")
            assert is_valid is False
            assert "Invalid capability" in error

    def test_validate_capability_not_granted(self, app):
        """Test validating capability that was not granted."""
        with app.app_context():
            is_valid, error = validate_plugin_capability("test-plugin", 1, "project:read")
            assert is_valid is False
            assert "does not have capability" in error

    def test_validate_multiple_capabilities(self, app):
        """Test validating multiple capabilities for same plugin."""
        with app.app_context():
            CapabilityStore.grant("test-plugin", 1, "project:read")
            CapabilityStore.grant("test-plugin", 1, "project:write")

            # One should pass
            is_valid, error = validate_plugin_capability("test-plugin", 1, "project:read")
            assert is_valid is True

            # Other should pass
            is_valid, error = validate_plugin_capability("test-plugin", 1, "project:write")
            assert is_valid is True

            # Third should fail
            is_valid, error = validate_plugin_capability("test-plugin", 1, "project:delete")
            assert is_valid is False
