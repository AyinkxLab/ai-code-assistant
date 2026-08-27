"""Tests for plugin manifest and registry."""

import json
import tempfile
from pathlib import Path

import pytest

from app.services.plugins import (
    ManifestValidationError,
    Plugin,
    PluginError,
    PluginManifest,
    PluginRegistrationError,
    PluginRegistry,
)


class TestPluginManifest:
    """Test plugin manifest parsing and validation."""

    def test_manifest_valid_minimal(self):
        """Test valid manifest with minimal fields."""
        data = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "description": "A test plugin",
            "author": "Test Author",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": ["PROJECT_READ"],
        }
        manifest = PluginManifest.from_dict(data)
        assert manifest.id == "test-plugin"
        assert manifest.name == "Test Plugin"
        assert manifest.version == "0.1.0"
        assert manifest.capabilities == ["PROJECT_READ"]
        assert manifest.permissions == []
        assert manifest.dependencies == []

    def test_manifest_valid_full(self):
        """Test valid manifest with all fields."""
        data = {
            "id": "stellar-tools",
            "name": "Stellar Developer Tools",
            "version": "1.0.0",
            "description": "Stellar/Soroban analysis",
            "author": "AI Code Assistant Team",
            "entry_point": "plugins.stellar_tools.plugin:StellarPlugin",
            "compatibility": ">=0.8.0",
            "capabilities": ["PROJECT_READ", "STELLAR_READ", "AI_ACCESS"],
            "permissions": ["read:project:files", "read:stellar:networks"],
            "dependencies": ["stellar-sdk>=11.0.0"],
            "configuration": {"enabled_networks": ["testnet"]},
        }
        manifest = PluginManifest.from_dict(data)
        assert manifest.id == "stellar-tools"
        assert manifest.name == "Stellar Developer Tools"
        assert len(manifest.capabilities) == 3
        assert manifest.dependencies == ["stellar-sdk>=11.0.0"]
        assert manifest.configuration["enabled_networks"] == ["testnet"]

    def test_manifest_missing_required_field(self):
        """Test manifest with missing required field."""
        data = {
            "id": "test-plugin",
            "name": "Test Plugin",
            # Missing version
            "description": "A test plugin",
            "author": "Test Author",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": ["PROJECT_READ"],
        }
        with pytest.raises(ManifestValidationError) as exc_info:
            PluginManifest.from_dict(data)
        assert "Missing required field: version" in str(exc_info.value)

    def test_manifest_invalid_id_format(self):
        """Test manifest with invalid id format."""
        test_cases = [
            "TestPlugin",  # Uppercase not allowed
            "123plugin",  # Cannot start with number
            "test plugin",  # No spaces
            "test@plugin",  # No special chars
        ]
        for invalid_id in test_cases:
            data = {
                "id": invalid_id,
                "name": "Test Plugin",
                "version": "0.1.0",
                "description": "A test plugin",
                "author": "Test Author",
                "entry_point": "plugins.test:TestPlugin",
                "capabilities": ["PROJECT_READ"],
            }
            with pytest.raises(ManifestValidationError) as exc_info:
                PluginManifest.from_dict(data)
            assert "Invalid id format" in str(exc_info.value)

    def test_manifest_valid_id_formats(self):
        """Test manifest with valid id formats."""
        valid_ids = [
            "test-plugin",
            "test_plugin",
            "testplugin",
            "a",
            "test-123-plugin",
        ]
        for valid_id in valid_ids:
            data = {
                "id": valid_id,
                "name": "Test Plugin",
                "version": "0.1.0",
                "description": "A test plugin",
                "author": "Test Author",
                "entry_point": "plugins.test:TestPlugin",
                "capabilities": ["PROJECT_READ"],
            }
            manifest = PluginManifest.from_dict(data)
            assert manifest.id == valid_id

    def test_manifest_invalid_version(self):
        """Test manifest with invalid semantic version."""
        invalid_versions = [
            "1",
            "1.0",
            "v1.0.0",
            "01.02.03",
            "1.0.0.0",
        ]
        for invalid_version in invalid_versions:
            data = {
                "id": "test-plugin",
                "name": "Test Plugin",
                "version": invalid_version,
                "description": "A test plugin",
                "author": "Test Author",
                "entry_point": "plugins.test:TestPlugin",
                "capabilities": ["PROJECT_READ"],
            }
            with pytest.raises(ManifestValidationError) as exc_info:
                PluginManifest.from_dict(data)
            assert "Invalid version format" in str(exc_info.value)

    def test_manifest_valid_versions(self):
        """Test manifest with valid semantic versions."""
        valid_versions = [
            "0.1.0",
            "1.0.0",
            "1.2.3",
            "1.0.0-alpha",
            "1.0.0-beta.1",
            "1.0.0+build123",
            "1.0.0-alpha+build",
        ]
        for valid_version in valid_versions:
            data = {
                "id": "test-plugin",
                "name": "Test Plugin",
                "version": valid_version,
                "description": "A test plugin",
                "author": "Test Author",
                "entry_point": "plugins.test:TestPlugin",
                "capabilities": ["PROJECT_READ"],
            }
            manifest = PluginManifest.from_dict(data)
            assert manifest.version == valid_version

    def test_manifest_invalid_entry_point(self):
        """Test manifest with invalid entry_point format."""
        invalid_entry_points = [
            "module",  # Missing class name
            "module:",  # Missing class
            ":ClassName",  # Missing module
            "module.path@ClassName",  # Invalid separator
        ]
        for invalid_ep in invalid_entry_points:
            data = {
                "id": "test-plugin",
                "name": "Test Plugin",
                "version": "0.1.0",
                "description": "A test plugin",
                "author": "Test Author",
                "entry_point": invalid_ep,
                "capabilities": ["PROJECT_READ"],
            }
            with pytest.raises(ManifestValidationError) as exc_info:
                PluginManifest.from_dict(data)
            assert "Invalid entry_point format" in str(exc_info.value)

    def test_manifest_valid_entry_points(self):
        """Test manifest with valid entry_point formats."""
        valid_entry_points = [
            "plugins.test:TestPlugin",
            "my_plugin:MyPlugin",
            "nested.modules.plugin:PluginClass",
            "a:A",
        ]
        for valid_ep in valid_entry_points:
            data = {
                "id": "test-plugin",
                "name": "Test Plugin",
                "version": "0.1.0",
                "description": "A test plugin",
                "author": "Test Author",
                "entry_point": valid_ep,
                "capabilities": ["PROJECT_READ"],
            }
            manifest = PluginManifest.from_dict(data)
            assert manifest.entry_point == valid_ep

    def test_manifest_invalid_capability(self):
        """Test manifest with unknown capability."""
        data = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "description": "A test plugin",
            "author": "Test Author",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": ["UNKNOWN_CAPABILITY"],
        }
        with pytest.raises(ManifestValidationError) as exc_info:
            PluginManifest.from_dict(data)
        assert "Unknown capability" in str(exc_info.value)

    def test_manifest_empty_capabilities(self):
        """Test manifest with empty capabilities list."""
        data = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "description": "A test plugin",
            "author": "Test Author",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": [],
        }
        with pytest.raises(ManifestValidationError) as exc_info:
            PluginManifest.from_dict(data)
        assert "Capabilities must be a non-empty list" in str(exc_info.value)

    def test_manifest_valid_capabilities(self):
        """Test manifest with all valid capabilities."""
        valid_caps = [
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
        data = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "description": "A test plugin",
            "author": "Test Author",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": valid_caps,
        }
        manifest = PluginManifest.from_dict(data)
        assert len(manifest.capabilities) == len(valid_caps)

    def test_manifest_from_file(self):
        """Test loading manifest from JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_file = Path(tmpdir) / "manifest.json"
            manifest_data = {
                "id": "test-plugin",
                "name": "Test Plugin",
                "version": "0.1.0",
                "description": "A test plugin",
                "author": "Test Author",
                "entry_point": "plugins.test:TestPlugin",
                "capabilities": ["PROJECT_READ"],
            }
            with open(manifest_file, "w") as f:
                json.dump(manifest_data, f)

            manifest = PluginManifest.from_file(manifest_file)
            assert manifest.id == "test-plugin"

    def test_manifest_from_file_invalid_json(self):
        """Test loading manifest with invalid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_file = Path(tmpdir) / "manifest.json"
            with open(manifest_file, "w") as f:
                f.write("{invalid json")

            with pytest.raises(PluginError) as exc_info:
                PluginManifest.from_file(manifest_file)
            assert "Invalid JSON" in str(exc_info.value)

    def test_manifest_from_file_not_found(self):
        """Test loading manifest from non-existent file."""
        with pytest.raises(PluginError) as exc_info:
            PluginManifest.from_file("/nonexistent/manifest.json")
        assert "Manifest file not found" in str(exc_info.value)

    def test_manifest_to_dict(self):
        """Test converting manifest to dictionary."""
        data = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "description": "A test plugin",
            "author": "Test Author",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": ["PROJECT_READ"],
            "permissions": ["perm1"],
            "dependencies": ["dep1"],
            "configuration": {"key": "value"},
        }
        manifest = PluginManifest.from_dict(data)
        result = manifest.to_dict()
        assert result["id"] == "test-plugin"
        assert result["capabilities"] == ["PROJECT_READ"]
        assert result["configuration"]["key"] == "value"


class TestPlugin:
    """Test plugin class."""

    def test_plugin_has_capability(self):
        """Test checking if plugin has capability."""
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ", "PROJECT_WRITE"],
        )
        plugin = Plugin(manifest)
        assert plugin.has_capability("PROJECT_READ")
        assert plugin.has_capability("PROJECT_WRITE")
        assert not plugin.has_capability("ADMIN")

    def test_plugin_initial_state(self):
        """Test plugin initial state."""
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        plugin = Plugin(manifest)
        assert plugin.enabled
        assert plugin.module is None
        assert plugin.loaded_at is None

    def test_plugin_repr(self):
        """Test plugin string representation."""
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        plugin = Plugin(manifest)
        assert "test-plugin" in repr(plugin)
        assert "0.1.0" in repr(plugin)


class TestPluginRegistry:
    """Test plugin registry."""

    def test_registry_register_plugin(self):
        """Test registering a plugin."""
        registry = PluginRegistry()
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        plugin = Plugin(manifest)
        registry.register(plugin)
        assert len(registry) == 1
        assert registry.get("test-plugin") == plugin

    def test_registry_register_duplicate(self):
        """Test registering duplicate plugin."""
        registry = PluginRegistry()
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        plugin1 = Plugin(manifest)
        plugin2 = Plugin(manifest)
        registry.register(plugin1)
        with pytest.raises(PluginRegistrationError) as exc_info:
            registry.register(plugin2)
        assert "already registered" in str(exc_info.value)

    def test_registry_get_plugin(self):
        """Test getting plugin by ID."""
        registry = PluginRegistry()
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        plugin = Plugin(manifest)
        registry.register(plugin)
        assert registry.get("test-plugin") == plugin
        assert registry.get("nonexistent") is None

    def test_registry_list_all(self):
        """Test listing all plugins."""
        registry = PluginRegistry()
        for i in range(3):
            manifest = PluginManifest(
                id=f"plugin-{i}",
                name=f"Plugin {i}",
                version="0.1.0",
                description="Test",
                author="Test",
                entry_point="plugins.test:Test",
                capabilities=["PROJECT_READ"],
            )
            registry.register(Plugin(manifest))
        assert len(registry.list_all()) == 3

    def test_registry_enable_disable(self):
        """Test enabling and disabling plugins."""
        registry = PluginRegistry()
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        plugin = Plugin(manifest)
        registry.register(plugin)
        assert plugin.enabled
        registry.disable("test-plugin")
        assert not plugin.enabled
        registry.enable("test-plugin")
        assert plugin.enabled

    def test_registry_enable_nonexistent(self):
        """Test enabling non-existent plugin."""
        registry = PluginRegistry()
        with pytest.raises(PluginRegistrationError):
            registry.enable("nonexistent")

    def test_registry_validate_capability(self):
        """Test validating plugin capability."""
        registry = PluginRegistry()
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        plugin = Plugin(manifest)
        registry.register(plugin)
        assert registry.validate_capability("test-plugin", "PROJECT_READ")
        assert not registry.validate_capability("test-plugin", "PROJECT_WRITE")
        assert not registry.validate_capability("nonexistent", "PROJECT_READ")

    def test_registry_discover_plugins(self):
        """Test discovering plugins in directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two plugins
            for i in range(2):
                plugin_dir = Path(tmpdir) / f"plugin-{i}"
                plugin_dir.mkdir()
                manifest_file = plugin_dir / "manifest.json"
                manifest_data = {
                    "id": f"plugin-{i}",
                    "name": f"Plugin {i}",
                    "version": "0.1.0",
                    "description": "Test",
                    "author": "Test",
                    "entry_point": "plugins.test:Test",
                    "capabilities": ["PROJECT_READ"],
                }
                with open(manifest_file, "w") as f:
                    json.dump(manifest_data, f)

            registry = PluginRegistry()
            discovered = registry.discover(tmpdir)
            assert len(discovered) == 2

    def test_registry_discover_empty_directory(self):
        """Test discovering plugins in empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = PluginRegistry()
            discovered = registry.discover(tmpdir)
            assert len(discovered) == 0

    def test_registry_discover_invalid_manifest(self):
        """Test discovering with invalid manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir) / "bad-plugin"
            plugin_dir.mkdir()
            manifest_file = plugin_dir / "manifest.json"
            with open(manifest_file, "w") as f:
                json.dump({"id": "bad"}, f)  # Missing required fields

            registry = PluginRegistry()
            discovered = registry.discover(tmpdir)
            assert len(discovered) == 0  # Invalid manifest is skipped

    def test_registry_dispatch_event(self):
        """Test dispatching events."""
        registry = PluginRegistry()
        called = []

        def handler(event_type: str, data: dict):
            called.append((event_type, data))

        registry.subscribe("test-plugin", "project.created", handler)
        registry.dispatch("project.created", {"project_id": 123})
        assert len(called) == 1
        assert called[0] == ("project.created", {"project_id": 123})

    def test_registry_dispatch_event_error_isolated(self):
        """Test that event handler errors are isolated."""
        registry = PluginRegistry()
        results = []

        def bad_handler(event_type: str, data: dict):
            raise ValueError("Handler error")

        def good_handler(event_type: str, data: dict):
            results.append("success")

        registry.subscribe("plugin1", "project.created", bad_handler)
        registry.subscribe("plugin2", "project.created", good_handler)
        registry.dispatch("project.created", {})

        # Good handler should still be called
        assert results == ["success"]

    def test_registry_repr(self):
        """Test registry string representation."""
        registry = PluginRegistry()
        manifest = PluginManifest(
            id="test-plugin",
            name="Test",
            version="0.1.0",
            description="Test",
            author="Test",
            entry_point="plugins.test:Test",
            capabilities=["PROJECT_READ"],
        )
        registry.register(Plugin(manifest))
        assert "1 plugins" in repr(registry)
