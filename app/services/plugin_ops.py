"""Operator-facing plugin operations (business logic for the plugin CLI).

These helpers act on the persisted ``Plugin`` rows (the same store the plugin
management API and dispatch-time authorization read), so the CLI never
duplicates list/enable/disable logic or capability handling.

Security rules:

* **Local install only.** ``install_from_local_dir`` accepts a filesystem path
  (a directory containing ``manifest.json``, or the manifest file itself) and
  rejects anything that looks like a URL. It never fetches network resources.
* **No implicit capability grants.** Installing/enabling never touches
  ``CapabilityStore``; capabilities remain explicit and per-workspace.
* **No code execution.** Manifests are parsed/validated only; plugin modules
  are never loaded here.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.extensions import db
from app.models import Plugin
from app.services.plugins import (
    ManifestValidationError,
    PluginError,
    PluginManifest,
)

#: Anything with a URL scheme is refused as a "local path".
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
#: Looks like an scp-style or URL-ish remote (host:path or git@host).
_REMOTE_HINT_RE = re.compile(r"^(?:[^/@\s]+@)?[^/\s:@]+:[/~]")


class PluginNotFoundError(ValueError):
    """Raised when a plugin id does not exist in the database."""


def list_plugins() -> list[Plugin]:
    """Return all registered plugins, ordered by id."""
    return Plugin.query.order_by(Plugin.id).all()


def get_plugin(plugin_id: str) -> Plugin:
    """Return a plugin row by id or raise :class:`PluginNotFoundError`."""
    plugin = db.session.get(Plugin, plugin_id)
    if plugin is None:
        raise PluginNotFoundError(f"Plugin not found: {plugin_id}")
    return plugin


def set_plugin_enabled(plugin_id: str, enabled: bool) -> Plugin:
    """Enable or disable a plugin (operator scope) and persist the change.

    Enabling never creates capability grants; disabling never revokes them
    (per-workspace grants are revoked only through the workspace API).
    """
    plugin = get_plugin(plugin_id)
    plugin.enabled = bool(enabled)
    db.session.commit()
    return plugin


def _resolve_manifest_file(path: str) -> Path:
    """Return the ``manifest.json`` for a local plugin path, refusing URLs."""
    if not isinstance(path, str) or not path.strip():
        raise PluginError("A local plugin path is required.")
    value = path.strip()
    if _URL_SCHEME_RE.match(value) or _REMOTE_HINT_RE.match(value):
        raise PluginError("Remote/URL installation is not supported; use a local path.")
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        candidate = candidate / "manifest.json"
    return candidate


def install_from_local_dir(path: str) -> Plugin:
    """Validate a local plugin manifest directory and register the plugin.

    The plugin id is taken only from the validated manifest (identity binding).
    A conflicting entry point for an already-registered id is rejected. No
    capabilities are granted and no code is executed.
    """
    manifest_file = _resolve_manifest_file(path)
    if not manifest_file.is_file():
        raise PluginError(f"Manifest file not found: {manifest_file}")
    manifest = PluginManifest.from_file(manifest_file)

    existing = db.session.get(Plugin, manifest.id)
    if existing is not None:
        if existing.entry_point != manifest.entry_point:
            raise PluginError(
                f"Manifest entry_point does not match the registered plugin {manifest.id}."
            )
        return existing

    plugin = Plugin(
        id=manifest.id,
        name=manifest.name,
        version=manifest.version,
        description=manifest.description,
        author=manifest.author,
        entry_point=manifest.entry_point,
        capabilities=manifest.capabilities,
        permissions=manifest.permissions or [],
        dependencies=manifest.dependencies or [],
        compatibility=manifest.compatibility,
        configuration=manifest.configuration or {},
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


__all__ = [
    "ManifestValidationError",
    "PluginError",
    "PluginManifest",
    "PluginNotFoundError",
    "get_plugin",
    "install_from_local_dir",
    "list_plugins",
    "set_plugin_enabled",
]
