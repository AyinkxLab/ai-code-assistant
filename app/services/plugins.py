"""Plugin system core: manifest, registry, and lifecycle management.

This module provides:
- PluginManifest: Parse and validate plugin manifest files
- Plugin: Runtime plugin instance
- PluginRegistry: Central registration and management
- Custom exceptions for plugin system
"""

import importlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PluginError(Exception):
    """Base exception for plugin system."""

    pass


class ManifestValidationError(PluginError):
    """Plugin manifest validation failed."""

    pass


class PluginRegistrationError(PluginError):
    """Plugin registration failed."""

    pass


class PluginLoadError(PluginError):
    """Plugin load failed."""

    pass


@dataclass
class PluginManifest:
    """Parsed and validated plugin manifest."""

    id: str
    name: str
    version: str
    description: str
    author: str
    entry_point: str
    capabilities: list[str]
    compatibility: str = ">=0.8.0"
    permissions: list[str] | None = None
    dependencies: list[str] | None = None
    configuration: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Normalize defaults."""
        if self.permissions is None:
            self.permissions = []
        if self.dependencies is None:
            self.dependencies = []
        if self.configuration is None:
            self.configuration = {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        """Parse manifest from dictionary.

        Args:
            data: Manifest dictionary

        Returns:
            PluginManifest instance

        Raises:
            ManifestValidationError: If manifest is invalid
        """
        errors = []

        # Validate required fields
        required = ["id", "name", "version", "description", "author", "entry_point", "capabilities"]
        for field in required:
            if field not in data:
                errors.append(f"Missing required field: {field}")

        if errors:
            raise ManifestValidationError("; ".join(errors))

        # Validate id format
        if not re.match(r"^[a-z][a-z0-9_-]*$", data.get("id", "")):
            errors.append("Invalid id format: must start with lowercase letter, contain only lowercase letters, numbers, hyphens, underscores")

        # Validate version format (semantic versioning)
        if not _is_valid_semver(data.get("version", "")):
            errors.append("Invalid version format: must be semantic version (e.g., 0.1.0)")

        # Validate entry_point format
        if not re.match(r"^[a-zA-Z0-9_][a-zA-Z0-9_.:]*:[a-zA-Z_][a-zA-Z0-9_]*$", data.get("entry_point", "")):
            errors.append("Invalid entry_point format: must be 'module.path:ClassName'")

        # Validate capabilities
        capabilities = data.get("capabilities", [])
        valid_capabilities = {
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
        }
        if not isinstance(capabilities, list) or not capabilities:
            errors.append("Capabilities must be a non-empty list")
        else:
            for cap in capabilities:
                if cap not in valid_capabilities:
                    errors.append(f"Unknown capability: {cap}")

        if errors:
            raise ManifestValidationError("; ".join(errors))

        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            entry_point=data["entry_point"],
            capabilities=data["capabilities"],
            compatibility=data.get("compatibility", ">=0.8.0"),
            permissions=data.get("permissions", []),
            dependencies=data.get("dependencies", []),
            configuration=data.get("configuration", {}),
        )

    @classmethod
    def from_file(cls, manifest_path: str | Path) -> "PluginManifest":
        """Load manifest from JSON file.

        Args:
            manifest_path: Path to manifest.json

        Returns:
            PluginManifest instance

        Raises:
            ManifestValidationError: If manifest is invalid
            PluginError: If file cannot be read
        """
        try:
            with open(manifest_path, "r") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise PluginError(f"Invalid JSON in manifest: {e}") from e
        except FileNotFoundError as e:
            raise PluginError(f"Manifest file not found: {manifest_path}") from e
        except IOError as e:
            raise PluginError(f"Cannot read manifest file: {e}") from e

        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entry_point": self.entry_point,
            "capabilities": self.capabilities,
            "compatibility": self.compatibility,
            "permissions": self.permissions or [],
            "dependencies": self.dependencies or [],
            "configuration": self.configuration or {},
        }


class Plugin:
    """Runtime plugin instance."""

    def __init__(self, manifest: PluginManifest):
        """Initialize plugin.

        Args:
            manifest: Parsed plugin manifest
        """
        self.manifest = manifest
        self.module: ModuleType | None = None
        self.enabled = True
        self.loaded_at: datetime | None = None

    def load(self, app_config: dict[str, Any] | None = None) -> None:
        """Load plugin module dynamically.

        Args:
            app_config: Application configuration for context

        Raises:
            PluginLoadError: If plugin cannot be loaded
        """
        try:
            module_path, class_name = self.manifest.entry_point.split(":")
            self.module = importlib.import_module(module_path)
            if not hasattr(self.module, class_name):
                raise PluginLoadError(f"Class {class_name} not found in module {module_path}")
            self.loaded_at = datetime.utcnow()
            logger.info(f"Loaded plugin: {self.manifest.id}")
        except ImportError as e:
            raise PluginLoadError(f"Cannot import plugin module: {e}") from e
        except Exception as e:
            raise PluginLoadError(f"Error loading plugin {self.manifest.id}: {e}") from e

    def unload(self) -> None:
        """Unload plugin safely."""
        self.module = None
        self.loaded_at = None
        logger.info(f"Unloaded plugin: {self.manifest.id}")

    def has_capability(self, capability: str) -> bool:
        """Check if plugin has capability.

        Args:
            capability: Capability to check (e.g., "PROJECT_READ")

        Returns:
            True if plugin has capability
        """
        return capability in self.manifest.capabilities

    def get_instance(self, app: Any | None = None) -> Any:
        """Get instantiated plugin class.

        Args:
            app: Flask app instance for plugin context

        Returns:
            Instance of plugin class

        Raises:
            PluginLoadError: If plugin not loaded
        """
        if not self.module:
            raise PluginLoadError(f"Plugin {self.manifest.id} not loaded")

        _, class_name = self.manifest.entry_point.split(":")
        plugin_class = getattr(self.module, class_name)
        return plugin_class(app=app, manifest=self.manifest) if app else plugin_class(manifest=self.manifest)

    def __repr__(self) -> str:
        """String representation."""
        return f"<Plugin {self.manifest.id} v{self.manifest.version}>"


class PluginRegistry:
    """Central plugin registration and management."""

    def __init__(self) -> None:
        """Initialize registry."""
        self._plugins: dict[str, Plugin] = {}
        self._hooks: dict[str, list[Callable]] = {}

    def register(self, plugin: Plugin) -> None:
        """Register plugin.

        Args:
            plugin: Plugin to register

        Raises:
            PluginRegistrationError: If plugin already registered or invalid
        """
        if plugin.manifest.id in self._plugins:
            raise PluginRegistrationError(f"Plugin {plugin.manifest.id} already registered")
        self._plugins[plugin.manifest.id] = plugin
        logger.info(f"Registered plugin: {plugin.manifest.id}")

    def discover(self, plugin_dir: str | Path) -> list[PluginManifest]:
        """Discover plugins in directory.

        Args:
            plugin_dir: Directory containing plugin folders

        Returns:
            List of discovered manifests
        """
        plugin_dir = Path(plugin_dir)
        manifests = []

        if not plugin_dir.exists():
            logger.warning(f"Plugin directory does not exist: {plugin_dir}")
            return manifests

        for item in plugin_dir.iterdir():
            if not item.is_dir() or item.name.startswith("_") or item.name.startswith("."):
                continue

            manifest_file = item / "manifest.json"
            if not manifest_file.exists():
                logger.debug(f"No manifest found in: {item}")
                continue

            try:
                manifest = PluginManifest.from_file(manifest_file)
                manifests.append(manifest)
                logger.debug(f"Discovered plugin: {manifest.id}")
            except ManifestValidationError as e:
                logger.warning(f"Invalid manifest in {item}: {e}")
            except PluginError as e:
                logger.warning(f"Error reading manifest from {item}: {e}")

        return manifests

    def get(self, plugin_id: str) -> Plugin | None:
        """Look up plugin by ID.

        Args:
            plugin_id: Plugin identifier

        Returns:
            Plugin instance or None
        """
        return self._plugins.get(plugin_id)

    def list_all(self) -> list[Plugin]:
        """List all registered plugins.

        Returns:
            List of all plugins
        """
        return list(self._plugins.values())

    def list_enabled(self) -> list[Plugin]:
        """List all enabled plugins.

        Returns:
            List of enabled plugins
        """
        return [p for p in self._plugins.values() if p.enabled]

    def enable(self, plugin_id: str) -> None:
        """Enable plugin.

        Args:
            plugin_id: Plugin identifier

        Raises:
            PluginRegistrationError: If plugin not found
        """
        plugin = self.get(plugin_id)
        if not plugin:
            raise PluginRegistrationError(f"Plugin not found: {plugin_id}")
        plugin.enabled = True
        logger.info(f"Enabled plugin: {plugin_id}")

    def disable(self, plugin_id: str) -> None:
        """Disable plugin.

        Args:
            plugin_id: Plugin identifier

        Raises:
            PluginRegistrationError: If plugin not found
        """
        plugin = self.get(plugin_id)
        if not plugin:
            raise PluginRegistrationError(f"Plugin not found: {plugin_id}")
        plugin.enabled = False
        logger.info(f"Disabled plugin: {plugin_id}")

    def validate_capability(self, plugin_id: str, capability: str) -> bool:
        """Check if plugin has capability.

        Args:
            plugin_id: Plugin identifier
            capability: Capability to check

        Returns:
            True if plugin has capability
        """
        plugin = self.get(plugin_id)
        if not plugin:
            return False
        return plugin.has_capability(capability)

    def subscribe(self, plugin_id: str, event_type: str, handler: Callable) -> None:
        """Subscribe plugin to event type.

        Args:
            plugin_id: Plugin identifier
            event_type: Event type (e.g., "project.created")
            handler: Callable to handle event
        """
        if event_type not in self._hooks:
            self._hooks[event_type] = []
        self._hooks[event_type].append((plugin_id, handler))
        logger.debug(f"Subscribed plugin {plugin_id} to {event_type}")

    def dispatch(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Dispatch event to subscribed plugins.

        Failures are isolated - one plugin failure does not affect others.

        Args:
            event_type: Event type (e.g., "project.created")
            data: Event data dictionary
        """
        if not data:
            data = {}

        handlers = self._hooks.get(event_type, [])
        for plugin_id, handler in handlers:
            try:
                handler(event_type=event_type, data=data)
            except Exception as e:
                logger.error(f"Error dispatching event {event_type} for plugin {plugin_id}: {e}", exc_info=True)

    def __len__(self) -> int:
        """Number of registered plugins."""
        return len(self._plugins)

    def __repr__(self) -> str:
        """String representation."""
        return f"<PluginRegistry with {len(self._plugins)} plugins>"


def _is_valid_semver(version: str) -> bool:
    """Check if version string is valid semantic version.

    Args:
        version: Version string to check

    Returns:
        True if valid semver
    """
    pattern = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d?)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
    return bool(re.match(pattern, version))
