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

from flask import current_app

from app.extensions import db
from app.models import Plugin, PluginLockEntry, Workspace
from app.services.plugin_catalog import CatalogEntry, load_plugin_catalog
from app.services.plugin_compat import enforce_compatible
from app.services.plugin_lockfile import LockfileError, normalize_integrity
from app.services.plugin_signatures import (
    VERIFY_IF_PRESENT,
    enforce_publisher_trust,
)
from app.services.plugins import (
    ManifestValidationError,
    PluginError,
    PluginManifest,
    PluginRegistry,
)

#: Anything with a URL scheme is refused as a "local path".
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
#: Looks like an scp-style or URL-ish remote (host:path or git@host).
_REMOTE_HINT_RE = re.compile(r"^(?:[^/@\s]+@)?[^/\s:@]+:[/~]")


class PluginNotFoundError(ValueError):
    """Raised when a plugin id does not exist in the database."""


class PluginUpdateError(ValueError):
    """Raised when a manifest cannot be applied to a registered plugin."""


class PluginEntryPointMismatchError(PluginUpdateError):
    """Raised when an update would retarget a registered plugin's entry point."""


class PluginDependencyResolutionError(ValueError):
    """Raised when a plugin cannot be enabled because dependencies are unsatisfied."""


class RemoteInstallError(PluginError):
    """Raised when a catalog install is refused before registration."""


def validate_catalog_install(catalog, entry) -> tuple[str, ...]:
    """Validate a catalog entry's transitive dependencies before writing.

    Catalog entries are walked recursively so a cyclic graph is refused before
    an install registers metadata. Dependencies that are not catalogued are not
    required to be installed yet; enable-time resolution remains unchanged.

    Raises:
        RemoteInstallError: When the catalog graph is cyclic, unparsable, or a
            catalogued dependency has no satisfying version.
    """
    from app.services.plugin_deps import PluginDependencyError, validate_catalog_dependencies

    try:
        return validate_catalog_dependencies(catalog, entry)
    except PluginDependencyError as exc:
        raise RemoteInstallError(f"Cannot install plugin {entry.plugin_id}: {exc.message}") from exc


def list_plugins() -> list[Plugin]:
    """Return all registered plugins, ordered by id."""
    return Plugin.query.order_by(Plugin.id).all()


def discover_plugins() -> list[dict]:
    """Return discoverable-but-not-registered plugin manifests (#172).

    Reads ``PLUGIN_DISCOVERY_DIR`` (a local directory of plugin folders, each
    containing a ``manifest.json``), validates each manifest, and returns only
    the plugins that are not already registered in the database. The operation
    is read-only: it never installs, loads, or mutates plugin state, and an
    absent/unreadable directory simply yields an empty list.
    """
    configured = current_app.config.get("PLUGIN_DISCOVERY_DIR")
    if not configured:
        return []

    manifest_list = PluginRegistry().discover(Path(configured))
    registered_ids = {row.id for row in Plugin.query.all()}
    discovered = [
        {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "capabilities": list(manifest.capabilities or []),
        }
        for manifest in manifest_list
        if manifest.id not in registered_ids
    ]
    discovered.sort(key=lambda item: item["id"])
    return discovered


def get_plugin(plugin_id: str) -> Plugin:
    """Return a plugin row by id or raise :class:`PluginNotFoundError`."""
    plugin = db.session.get(Plugin, plugin_id)
    if plugin is None:
        raise PluginNotFoundError(f"Plugin not found: {plugin_id}")
    return plugin


def set_plugin_enabled(plugin_id: str, enabled: bool) -> Plugin:
    """Enable or disable a plugin (operator scope) and persist the change.

    Enabling resolves the plugin's declared dependencies first: registered
    plugin dependencies must exist and be enabled, and Python package
    dependencies must be installed at a satisfying version. When resolution
    fails the plugin stays disabled and :class:`PluginDependencyResolutionError`
    is raised. Enabling never creates capability grants; disabling never
    revokes them (per-workspace grants are revoked only through the workspace
    API).
    """
    plugin = get_plugin(plugin_id)
    if enabled:
        resolve_plugin_dependencies(plugin)
    plugin.enabled = bool(enabled)
    db.session.commit()
    return plugin


def resolve_plugin_dependencies(plugin: Plugin) -> None:
    """Resolve a persisted plugin's declared dependencies.

    Raises :class:`PluginDependencyResolutionError` (never mutating the plugin)
    when a required dependency is missing, disabled, version-incompatible, or
    cyclic. Used by the enable path and available to the HTTP API so workspace
    enable requests surface the same clear errors.
    """
    from app.services.plugin_deps import DependencyResolver, PluginDependencyError, PluginRef

    def _ref(plugin_id: str) -> PluginRef | None:
        row = db.session.get(Plugin, plugin_id)
        if row is None:
            return None
        return PluginRef(
            id=row.id,
            version=row.version,
            enabled=row.enabled,
            dependencies=tuple(row.dependencies or ()),
        )

    resolver = DependencyResolver(_ref)
    try:
        resolver.resolve(_ref(plugin.id))
    except PluginDependencyError as exc:
        raise PluginDependencyResolutionError(
            f"Cannot enable plugin {plugin.id}: {exc.message}"
        ) from exc


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
    manifest = PluginManifest.from_file(
        manifest_file,
        trusted_publishers=current_app.config["PLUGIN_TRUSTED_KEYS"],
        trust_policy=current_app.config["PLUGIN_TRUST_POLICY"],
    )

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
        trust_state=manifest.trust_state,
        trust_publisher=manifest.trust_publisher,
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


def install_from_catalog(
    plugin_id: str,
    version: str | None = None,
    *,
    config=None,
) -> tuple[Plugin, CatalogEntry]:
    """Register a plugin resolved from the operator-controlled catalog (#387/#390).

    The active ``PLUGIN_TRUST_POLICY`` is enforced **before** anything is
    written: ``required`` refuses unsigned entries, while ``if-present`` accepts
    an unsigned entry as ``Unverified`` but still refuses forged or untrusted
    signatures. The declared compatibility range is enforced next, and only then
    is the global ``Plugin`` row created/reused.

    This is the shared remote-install path used by the CLI (and mirrors the API
    route), so the policy cannot drift between surfaces. No artifact is
    downloaded, no code is executed and no capability is granted.

    Args:
        plugin_id: Catalog plugin id.
        version: Optional exact version; newest when omitted.
        config: Config mapping to read from (defaults to the app config).

    Returns:
        ``(plugin, catalog_entry)`` for the registered plugin.

    Raises:
        CatalogUnavailableError/CatalogError/CatalogEntryNotFoundError: Catalog
            problems (propagated from the catalog loader).
        PublisherSignatureError: The active policy refused the signature.
        PluginTrustConfigError: The trust policy or key configuration is invalid.
        IncompatibleCompatibilityError/InvalidCompatibilityError: The plugin's
            compatibility range excludes the running app or is malformed.
        RemoteInstallError: The manifest conflicts with an existing registration.
    """
    source = config if config is not None else current_app.config
    catalog = load_plugin_catalog(source)
    entry = catalog.get(plugin_id, version)

    # Walk the catalog graph before any trust checks or writes. This catches
    # cycles across not-yet-installed dependencies without requiring them to be
    # installed; enable-time resolution still does that separately.
    validate_catalog_install(catalog, entry)

    trusted_publishers = source.get("PLUGIN_TRUSTED_KEYS") or {}
    # Schema validation is trust-policy agnostic; the policy is enforced below
    # via enforce_publisher_trust so a detached catalog signature is checked.
    manifest = PluginManifest.from_dict(
        entry.manifest,
        trusted_publishers=trusted_publishers,
        trust_policy=VERIFY_IF_PRESENT,
    )

    policy = source.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT)
    signature = entry.signature if entry.signature is not None else entry.manifest.get("signature")
    trust = enforce_publisher_trust(
        entry.manifest,
        signature,
        trusted_publishers=trusted_publishers,
        policy=policy,
    )

    enforce_compatible(manifest.compatibility, subject=f"Plugin {manifest.id}")

    # Persist only after every guard has passed.
    manifest.trust_state = trust.state
    manifest.trust_publisher = trust.publisher
    plugin = _register_manifest(manifest)
    return plugin, entry


def _register_manifest(manifest: PluginManifest) -> Plugin:
    """Create or reuse the global ``Plugin`` row for a validated manifest."""
    existing = db.session.get(Plugin, manifest.id)
    if existing is not None:
        if existing.entry_point != manifest.entry_point:
            raise RemoteInstallError(
                f"Manifest entry_point does not match the registered plugin {manifest.id}."
            )
        existing.version = manifest.version
        existing.trust_state = manifest.trust_state
        existing.trust_publisher = manifest.trust_publisher
        db.session.commit()
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
        trust_state=manifest.trust_state,
        trust_publisher=manifest.trust_publisher,
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


def update_plugin(plugin_id: str, manifest: PluginManifest) -> Plugin:
    """Apply a validated manifest to an already-registered plugin atomically.

    Updates are identity-bound like installs: the manifest id must equal
    ``plugin_id`` and the entry point must not change, so an update can never
    retarget an installed plugin at different code.

    The declared PEP 440 ``compatibility`` range is enforced against the
    running application *before* anything is written. An update whose range
    excludes this app version raises
    :class:`~app.services.plugin_compat.IncompatibleCompatibilityError` naming
    both the range and the current version, and leaves the registered plugin
    untouched. Invalid specifiers raise
    :class:`~app.services.plugin_compat.InvalidCompatibilityError`.

    Capability grants are intentionally not modified: they stay explicit and
    per-workspace, and are managed only through the capability endpoints.
    Any failure rolls the transaction back, so the prior version stays active.
    """
    plugin = get_plugin(plugin_id)

    if manifest.id != plugin_id:
        raise PluginUpdateError(
            f"Manifest id {manifest.id!r} does not match the plugin being updated "
            f"({plugin_id!r})."
        )
    if manifest.entry_point != plugin.entry_point:
        raise PluginEntryPointMismatchError(
            f"Manifest entry_point does not match the registered plugin {plugin_id}."
        )

    enforce_compatible(
        manifest.compatibility,
        subject=f"Plugin {plugin_id} {manifest.version}",
    )

    try:
        _apply_manifest(plugin, manifest)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return plugin


class AtomicUpdateError(PluginUpdateError):
    """A catalog update/rollback could not be applied without partial state."""


def _apply_manifest(plugin: Plugin, manifest: PluginManifest) -> None:
    """Copy validated manifest fields onto ``plugin`` without committing."""
    plugin.name = manifest.name
    plugin.version = manifest.version
    plugin.description = manifest.description
    plugin.author = manifest.author
    plugin.capabilities = list(manifest.capabilities)
    plugin.permissions = list(manifest.permissions or [])
    plugin.dependencies = list(manifest.dependencies or [])
    plugin.compatibility = manifest.compatibility
    plugin.configuration = dict(manifest.configuration or {})
    plugin.trust_state = manifest.trust_state
    plugin.trust_publisher = manifest.trust_publisher


def _validated_catalog_manifest(
    entry: CatalogEntry,
    *,
    trusted_publishers,
    policy: str,
) -> PluginManifest:
    """Validate a catalog entry's manifest and enforce the trust policy."""
    manifest = PluginManifest.from_dict(
        entry.manifest,
        trusted_publishers=trusted_publishers,
        trust_policy=VERIFY_IF_PRESENT,
    )
    signature = entry.signature if entry.signature is not None else entry.manifest.get("signature")
    trust = enforce_publisher_trust(
        entry.manifest,
        signature,
        trusted_publishers=trusted_publishers,
        policy=policy,
    )
    manifest.trust_state = trust.state
    manifest.trust_publisher = trust.publisher
    return manifest


def _upsert_lock_entry(
    plugin: Plugin,
    workspace_id: int,
    entry: CatalogEntry,
    integrity: str,
) -> None:
    """Mirror the resolved artifact into the workspace lock entry (no commit)."""
    row = PluginLockEntry.query.filter_by(
        workspace_id=workspace_id,
        plugin_id=plugin.id,
    ).first()
    if row is None:
        workspace = db.session.get(Workspace, workspace_id)
        owner_id = getattr(workspace, "user_id", None)
        if owner_id is None:
            raise AtomicUpdateError("Workspace not found for the plugin lock entry")
        row = PluginLockEntry(
            workspace_id=workspace_id,
            owner_id=owner_id,
            plugin_id=plugin.id,
        )
        db.session.add(row)
    row.version = entry.version
    row.source = entry.source
    row.integrity = integrity
    row.dependencies = list(entry.manifest.get("dependencies") or [])


def atomic_update_from_catalog(
    plugin_id: str,
    version: str | None = None,
    *,
    workspace_id: int | None = None,
    config=None,
    catalog=None,
) -> tuple[Plugin, CatalogEntry]:
    """Update an installed plugin to a catalog version in one transaction (#401).

    Every guard runs **before** any field is changed:

    * the manifest is schema-validated and identity-bound (same id/entry point);
    * the active ``PLUGIN_TRUST_POLICY`` verifies the publisher signature;
    * the catalog sha256 integrity is validated/normalized;
    * the declared compatibility range includes the running app.

    The plugin row (and, when ``workspace_id`` is supplied, its lock entry) are
    then swapped in a single commit. Any failure rolls the transaction back, so
    the prior version stays active and no partial state is left behind.
    """
    source = config if config is not None else current_app.config
    active_catalog = catalog if catalog is not None else load_plugin_catalog(source)
    entry = active_catalog.get(plugin_id, version)

    trusted_publishers = source.get("PLUGIN_TRUSTED_KEYS") or {}
    policy = source.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT)
    manifest = _validated_catalog_manifest(
        entry,
        trusted_publishers=trusted_publishers,
        policy=policy,
    )
    try:
        integrity = normalize_integrity(entry.integrity)
    except LockfileError as exc:
        raise AtomicUpdateError(f"Invalid artifact integrity: {exc}") from exc

    enforce_compatible(manifest.compatibility, subject=f"Plugin {manifest.id}")

    plugin = get_plugin(plugin_id)
    if manifest.id != plugin_id:
        raise PluginUpdateError(
            f"Manifest id {manifest.id!r} does not match the plugin being updated "
            f"({plugin_id!r})."
        )
    if manifest.entry_point != plugin.entry_point:
        raise PluginEntryPointMismatchError(
            f"Manifest entry_point does not match the registered plugin {plugin_id}."
        )

    try:
        _apply_manifest(plugin, manifest)
        if workspace_id is not None:
            _upsert_lock_entry(plugin, workspace_id, entry, integrity)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return plugin, entry


def atomic_rollback(
    plugin_id: str,
    manifest: PluginManifest,
    *,
    workspace_id: int | None = None,
) -> Plugin:
    """Apply a rollback manifest and its lock version in a single transaction.

    Used for the lock-entry rollback path: the target version comes from the
    workspace lockfile. A failure rolls back both the plugin row and the lock
    entry, leaving the current version active.
    """
    plugin = get_plugin(plugin_id)
    if manifest.id != plugin_id:
        raise PluginUpdateError(
            f"Manifest id {manifest.id!r} does not match the plugin being rolled back "
            f"({plugin_id!r})."
        )
    if manifest.entry_point != plugin.entry_point:
        raise PluginEntryPointMismatchError(
            f"Manifest entry_point does not match the registered plugin {plugin_id}."
        )
    enforce_compatible(manifest.compatibility, subject=f"Plugin {plugin_id} {manifest.version}")

    try:
        _apply_manifest(plugin, manifest)
        if workspace_id is not None:
            row = PluginLockEntry.query.filter_by(
                workspace_id=workspace_id,
                plugin_id=plugin_id,
            ).first()
            if row is not None:
                row.version = manifest.version
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return plugin


def rollback_from_catalog(
    plugin_id: str,
    *,
    workspace_id: int | None = None,
    config=None,
    catalog=None,
) -> tuple[Plugin, CatalogEntry]:
    """Roll a plugin back to the newest catalog version older than the current one.

    Uses the same atomic update path, so a failed rollback leaves the current
    version active.
    """
    from app.services.plugin_versions import is_newer, latest_version

    source = config if config is not None else current_app.config
    plugin = get_plugin(plugin_id)
    active_catalog = catalog if catalog is not None else load_plugin_catalog(source)
    older = [
        entry.version
        for entry in active_catalog.list_entries()
        if entry.plugin_id == plugin_id and is_newer(plugin.version, entry.version)
    ]
    target = latest_version(older)
    if target is None:
        raise AtomicUpdateError(
            f"No earlier version of plugin {plugin_id} is available in the catalog."
        )
    return atomic_update_from_catalog(
        plugin_id,
        target,
        workspace_id=workspace_id,
        config=source,
        catalog=active_catalog,
    )


class PluginReloadDisabledError(PluginError):
    """Plugin hot reload was requested outside development."""


def hot_reload_enabled(config=None) -> bool:
    """Return whether development-only plugin hot reload is enabled."""
    cfg = config or current_app.config
    return bool(cfg.get("DEBUG")) or str(cfg.get("APP_ENV", "")).strip().lower() in {
        "development",
        "testing",
    }


def reload_plugin(plugin_id: str, *, config=None) -> Plugin:
    """Reload a plugin's validated manifest from the discovery directory.

    Strictly development-only: production configuration refuses before any
    filesystem or database access. The manifest is re-read and applied through
    the normal identity-bound update path, so entry points cannot change and no
    capability grant is created.
    """
    cfg = config or current_app.config
    if not hot_reload_enabled(cfg):
        raise PluginReloadDisabledError("Plugin hot reload is disabled outside development.")

    get_plugin(plugin_id)
    discovery = Path(cfg.get("PLUGIN_DISCOVERY_DIR", "plugins"))
    manifest_path = discovery / plugin_id / "manifest.json"
    manifest = PluginManifest.from_file(manifest_path)
    if manifest.id != plugin_id:
        raise PluginUpdateError(
            f"Manifest id {manifest.id!r} does not match reloaded plugin {plugin_id!r}."
        )
    return update_plugin(plugin_id, manifest)


__all__ = [
    "AtomicUpdateError",
    "ManifestValidationError",
    "PluginDependencyResolutionError",
    "PluginError",
    "PluginManifest",
    "PluginNotFoundError",
    "PluginReloadDisabledError",
    "PluginUpdateError",
    "RemoteInstallError",
    "atomic_rollback",
    "atomic_update_from_catalog",
    "discover_plugins",
    "get_plugin",
    "hot_reload_enabled",
    "install_from_catalog",
    "install_from_local_dir",
    "list_plugins",
    "reload_plugin",
    "resolve_plugin_dependencies",
    "rollback_from_catalog",
    "set_plugin_enabled",
    "update_plugin",
    "validate_catalog_install",
]
