"""Plugin system core: manifest, registry, and lifecycle management.

This module provides:
- PluginManifest: Parse and validate plugin manifest files
- Plugin: Runtime plugin instance
- PluginRegistry: Central registration and management
- Custom exceptions for plugin system
"""

import base64
import binascii
import importlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.plugin_compat import is_valid_compatibility

logger = logging.getLogger(__name__)

TRUSTED = "Trusted"
UNVERIFIED = "Unverified"
INVALID = "Invalid"
VERIFY_IF_PRESENT = "if-present"
VERIFY_REQUIRED = "required"

#: The declared plugin manifest contract. Runtime validation below is driven by
#: this schema (see :func:`load_manifest_schema`) so the two cannot silently
#: drift apart.
MANIFEST_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "plugins" / "plugin.schema.json"

#: Manifest keys that are produced by the runtime rather than declared in the
#: schema. They are tolerated on input so a manifest serialized by
#: :meth:`PluginManifest.to_dict` can be re-validated.
_RUNTIME_MANAGED_FIELDS = frozenset({"trust_state", "trust_publisher"})


@lru_cache(maxsize=1)
def load_manifest_schema() -> dict[str, Any]:
    """Load the published plugin manifest JSON Schema.

    The schema is the single declared contract for ``manifest.json`` files; the
    runtime validator derives its field rules from it. The result is cached for
    the lifetime of the process.
    """
    with MANIFEST_SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _manifest_property(field: str) -> dict[str, Any]:
    """Return the schema rules declared for a single manifest field."""
    return load_manifest_schema()["properties"][field]


def _matches_schema_pattern(field: str, value: str) -> bool:
    """Return ``True`` when ``value`` matches the schema's ``pattern`` for ``field``."""
    pattern = _manifest_property(field).get("pattern")
    return pattern is None or re.match(pattern, value) is not None


def _validate_string_field(field: str, value: Any) -> list[str]:
    """Validate an optional string field against the schema's length bounds."""
    rules = _manifest_property(field)
    if not isinstance(value, str):
        return [f"Invalid {field}: must be a string"]
    errors: list[str] = []
    min_length = rules.get("minLength", 0)
    max_length = rules.get("maxLength")
    if len(value) < min_length:
        errors.append(f"Invalid {field}: must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        errors.append(f"Invalid {field}: must be at most {max_length} characters")
    return errors


def _validate_string_list(field: str, value: Any) -> list[str]:
    """Validate an optional string-array field declared in the schema."""
    rules = _manifest_property(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return [f"Invalid {field}: must be a list of strings"]
    if rules.get("uniqueItems") and len(set(value)) != len(value):
        return [f"Invalid {field}: must not contain duplicates"]
    return []


def _validate_manifest_contract(data: dict[str, Any]) -> list[str]:
    """Validate ``data`` against the declared manifest schema.

    Returns a list of human-readable errors (empty when valid). Rules and their
    bounds (patterns, enum, lengths, uniqueness) are read from
    ``plugins/plugin.schema.json`` so the schema stays the single source of
    truth for the manifest contract.
    """
    schema = load_manifest_schema()
    properties = schema["properties"]

    # Required fields are validated first: later checks assume they exist.
    errors = [
        f"Missing required field: {field}" for field in schema["required"] if field not in data
    ]
    if errors:
        return errors

    # The schema sets ``additionalProperties: false``; runtime-managed trust
    # fields are tolerated so ``to_dict()`` output round-trips.
    if schema.get("additionalProperties") is False:
        errors.extend(
            f"Unknown manifest field: {field}"
            for field in data
            if field not in properties and field not in _RUNTIME_MANAGED_FIELDS
        )

    plugin_id = data["id"]
    if not isinstance(plugin_id, str) or not _matches_schema_pattern("id", plugin_id):
        errors.append(
            "Invalid id format: must start with lowercase letter, "
            "contain only lowercase letters, numbers, hyphens, underscores"
        )
    else:
        max_length = _manifest_property("id").get("maxLength")
        if max_length is not None and len(plugin_id) > max_length:
            errors.append(f"Invalid id format: must be at most {max_length} characters")

    version = data["version"]
    if not _is_valid_semver(version):
        errors.append("Invalid version format: must be semantic version (e.g., 0.1.0)")

    entry_point = data["entry_point"]
    if not isinstance(entry_point, str) or not _matches_schema_pattern("entry_point", entry_point):
        errors.append("Invalid entry_point format: must be 'module.path:ClassName'")

    for field in ("name", "description", "author"):
        errors.extend(_validate_string_field(field, data[field]))

    capabilities = data["capabilities"]
    cap_rules = properties["capabilities"]
    allowed_capabilities = cap_rules["items"]["enum"]
    if not isinstance(capabilities, list) or len(capabilities) < cap_rules.get("minItems", 1):
        errors.append("Capabilities must be a non-empty list")
    else:
        seen: set[str] = set()
        for cap in capabilities:
            if not isinstance(cap, str) or cap not in allowed_capabilities:
                errors.append(f"Unknown capability: {cap}")
                continue
            if cap in seen:
                errors.append(f"Duplicate capability: {cap}")
            seen.add(cap)

    for field in ("permissions", "dependencies"):
        value = data.get(field)
        if value is not None:
            errors.extend(_validate_string_list(field, value))

    configuration = data.get("configuration")
    if configuration is not None and not isinstance(configuration, dict):
        errors.append("Invalid configuration: must be an object")

    return errors


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


#: Optional lifecycle hook names a plugin class may implement.
#:
#: Hooks are invoked on registry ``enable``/``disable``/``uninstall`` **only for
#: plugins already loaded in-process** (``plugin.module is not None``) — the
#: management API never loads arbitrary plugin code on its own. Hook failures
#: are isolated: the state change still happens and the registry stays
#: consistent.
LIFECYCLE_HOOKS = ("on_enable", "on_disable", "on_uninstall")


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
    compatibility: str | None = None
    permissions: list[str] | None = None
    dependencies: list[str] | None = None
    configuration: dict[str, Any] | None = None
    signature: dict[str, str] | None = None
    trust_state: str = UNVERIFIED
    trust_publisher: str | None = None

    def __post_init__(self) -> None:
        """Normalize defaults."""
        if self.permissions is None:
            self.permissions = []
        if self.dependencies is None:
            self.dependencies = []
        if self.configuration is None:
            self.configuration = {}

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        trusted_publishers: dict[str, str | bytes] | None = None,
        trust_policy: str = VERIFY_IF_PRESENT,
    ) -> "PluginManifest":
        """Parse manifest from dictionary.

        Args:
            data: Manifest dictionary

        Returns:
            PluginManifest instance

        Raises:
            ManifestValidationError: If manifest is invalid
        """
        if not isinstance(data, dict):
            raise ManifestValidationError("Plugin manifest must be a JSON object")

        errors = _validate_manifest_contract(data)

        # Validate the PEP 440 compatibility specifier (e.g. ">=0.8.0"). The
        # field is optional; when omitted the plugin supports any app version.
        # PEP 440 cannot be expressed in the JSON Schema, so this runtime check
        # is intentionally stricter than the declared `compatibility` string.
        compatibility = data.get("compatibility")
        if compatibility is not None and not is_valid_compatibility(compatibility):
            errors.append(f"Invalid compatibility specifier: {compatibility}")

        if errors:
            raise ManifestValidationError("; ".join(errors))

        signature = data.get("signature")
        signature_error = _validate_signature_metadata(signature)

        if trust_policy not in (VERIFY_IF_PRESENT, VERIFY_REQUIRED):
            raise ManifestValidationError(f"Invalid plugin trust policy: {trust_policy}")
        trust_state, trust_publisher = _verify_signature(
            data,
            signature,
            trusted_publishers or {},
        )
        if trust_policy == VERIFY_REQUIRED and trust_state != TRUSTED:
            raise ManifestValidationError(
                f"Plugin manifest trust verification failed: {trust_state}"
            )
        if signature_error:
            raise ManifestValidationError(signature_error)

        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            entry_point=data["entry_point"],
            capabilities=data["capabilities"],
            compatibility=compatibility,
            permissions=data.get("permissions", []),
            dependencies=data.get("dependencies", []),
            configuration=data.get("configuration", {}),
            signature=signature,
            trust_state=trust_state,
            trust_publisher=trust_publisher,
        )

    @classmethod
    def from_file(
        cls,
        manifest_path: str | Path,
        *,
        trusted_publishers: dict[str, str | bytes] | None = None,
        trust_policy: str = VERIFY_IF_PRESENT,
    ) -> "PluginManifest":
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
            with open(manifest_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise PluginError(f"Invalid JSON in manifest: {e}") from e
        except FileNotFoundError as e:
            raise PluginError(f"Manifest file not found: {manifest_path}") from e
        except OSError as e:
            raise PluginError(f"Cannot read manifest file: {e}") from e

        return cls.from_dict(
            data,
            trusted_publishers=trusted_publishers,
            trust_policy=trust_policy,
        )

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
            "signature": self.signature,
            "trust_state": self.trust_state,
            "trust_publisher": self.trust_publisher,
        }


def _validate_signature_metadata(signature: Any) -> str | None:
    """Validate the shape of optional signature metadata."""
    if signature is None:
        return None
    if not isinstance(signature, dict):
        return "Invalid signature: must be an object"
    if set(signature) != {"publisher", "algorithm", "value"}:
        return "Invalid signature: expected publisher, algorithm, and value"
    if not all(isinstance(signature.get(key), str) and signature[key] for key in signature):
        return "Invalid signature: publisher, algorithm, and value must be non-empty strings"
    if signature["algorithm"] != "ed25519":
        return "Invalid signature: algorithm must be ed25519"
    return None


def _canonical_manifest_body(data: dict[str, Any]) -> bytes:
    """Return the stable JSON representation signed by plugin publishers."""
    body = {key: value for key, value in data.items() if key != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _decode_public_key(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if value.startswith("-----BEGIN"):
        from cryptography.hazmat.primitives import serialization

        return serialization.load_pem_public_key(value.encode()).public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    return base64.b64decode(value, validate=True)


def _verify_signature(
    data: dict[str, Any],
    signature: Any,
    trusted_publishers: dict[str, str | bytes],
) -> tuple[str, str | None]:
    if signature is None:
        return UNVERIFIED, None
    if _validate_signature_metadata(signature):
        return INVALID, signature.get("publisher") if isinstance(signature, dict) else None
    publisher = signature["publisher"]
    key_value = trusted_publishers.get(publisher)
    if key_value is None:
        return INVALID, publisher
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_decode_public_key(key_value))
        public_key.verify(
            base64.b64decode(signature["value"], validate=True),
            _canonical_manifest_body(data),
        )
    except (ValueError, TypeError, binascii.Error, InvalidSignature):
        return INVALID, publisher
    return TRUSTED, publisher


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
            self.loaded_at = datetime.now(UTC)
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

    def invoke_hook(self, hook: str, app: Any | None = None) -> dict[str, Any]:
        """Invoke an optional lifecycle hook on the loaded plugin instance.

        ``hook`` is one of :data:`LIFECYCLE_HOOKS`. Hooks are best-effort and
        isolated: an absent hook, an unloaded plugin, or a raising hook never
        propagates. Returns ``{"hook", "called", "ok", "error"}`` so callers can
        surface failures without breaking state.
        """
        if self.module is None:
            return {"hook": hook, "called": False, "ok": True, "error": None}
        try:
            instance = self.get_instance(app=app)
        except Exception as exc:  # never let an instance error break a lifecycle change
            logger.error(
                "Could not instantiate plugin %s for hook %s: %s",
                self.manifest.id,
                hook,
                exc,
                exc_info=True,
            )
            return {"hook": hook, "called": False, "ok": False, "error": str(exc)}
        method = getattr(instance, hook, None)
        if not callable(method):
            return {"hook": hook, "called": False, "ok": True, "error": None}
        try:
            method()
        except Exception as exc:
            logger.error(
                "Plugin %s hook %s failed: %s",
                self.manifest.id,
                hook,
                exc,
                exc_info=True,
            )
            return {"hook": hook, "called": True, "ok": False, "error": str(exc)}
        return {"hook": hook, "called": True, "ok": True, "error": None}

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
        if app:
            return plugin_class(app=app, manifest=self.manifest)
        return plugin_class(manifest=self.manifest)

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
        """Enable plugin and run its optional ``on_enable`` hook.

        Args:
            plugin_id: Plugin identifier

        Raises:
            PluginRegistrationError: If plugin not found
        """
        plugin = self.get(plugin_id)
        if not plugin:
            raise PluginRegistrationError(f"Plugin not found: {plugin_id}")
        if plugin.enabled:
            return
        plugin.enabled = True
        self._run_hook(plugin, "on_enable")
        logger.info(f"Enabled plugin: {plugin_id}")

    def disable(self, plugin_id: str) -> None:
        """Disable plugin and run its optional ``on_disable`` hook.

        Args:
            plugin_id: Plugin identifier

        Raises:
            PluginRegistrationError: If plugin not found
        """
        plugin = self.get(plugin_id)
        if not plugin:
            raise PluginRegistrationError(f"Plugin not found: {plugin_id}")
        if not plugin.enabled:
            return
        plugin.enabled = False
        self._run_hook(plugin, "on_disable")
        logger.info(f"Disabled plugin: {plugin_id}")

    def uninstall(self, plugin_id: str) -> None:
        """Uninstall a plugin from the registry.

        Runs the optional ``on_uninstall`` hook first, then removes the plugin.
        A raising hook never blocks removal, so the registry always ends
        consistent.

        Args:
            plugin_id: Plugin identifier

        Raises:
            PluginRegistrationError: If plugin not found
        """
        plugin = self.get(plugin_id)
        if not plugin:
            raise PluginRegistrationError(f"Plugin not found: {plugin_id}")
        self._run_hook(plugin, "on_uninstall")
        del self._plugins[plugin_id]
        logger.info(f"Uninstalled plugin: {plugin_id}")

    def _run_hook(self, plugin: Plugin, hook: str) -> dict[str, Any]:
        """Invoke a lifecycle hook, logging (never propagating) failures."""
        result = plugin.invoke_hook(hook)
        if result.get("ok") is False:
            logger.warning(
                "Plugin %s %s reported a problem: %s",
                plugin.manifest.id,
                hook,
                result.get("error"),
            )
            self._record_hook_error(plugin.manifest.id, hook, result.get("error"))
        return result

    def _record_hook_error(self, plugin_id: str, hook: str, message: Any) -> None:
        """Best-effort bounded error report when running inside an app context."""
        try:
            from flask import has_app_context

            if not has_app_context():
                return
            from app.services.plugin_errors import record_plugin_error

            record_plugin_error(plugin_id, f"hook:{hook}", message=str(message))
        except Exception:  # pragma: no cover - never let recording break lifecycle
            logger.debug("Could not record plugin hook error report", exc_info=True)

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
                logger.error(
                    f"Error dispatching event {event_type} for plugin {plugin_id}: {e}",
                    exc_info=True,
                )

    def __len__(self) -> int:
        """Number of registered plugins."""
        return len(self._plugins)

    def __repr__(self) -> str:
        """String representation."""
        return f"<PluginRegistry with {len(self._plugins)} plugins>"


def _is_valid_semver(version: str) -> bool:
    """Check if version string is valid semantic version.

    The accepted pattern is read from the manifest schema's ``version``
    constraint so the runtime check cannot drift from the declared contract.

    Args:
        version: Version string to check

    Returns:
        True if valid semver
    """
    return isinstance(version, str) and _matches_schema_pattern("version", version)
