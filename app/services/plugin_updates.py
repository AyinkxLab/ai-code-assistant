"""Plugin update discovery, plans and owner notifications (#403/#404).

The operator-controlled catalog is the source of truth for available versions.
This module answers three questions for the UI/API without ever loading plugin
code or downloading artifacts:

* which newer, compatible version is available (and its trust state);
* which version a rollback would target;
* which workspace owners should be notified about a newly available version.

Notifications go through the single ``notify()`` service, so per-user
preferences are honoured, and are deduplicated per ``(plugin, version)`` so a
newer version fires exactly once.
"""

from __future__ import annotations

from typing import Any

from flask import current_app, has_app_context

from app.extensions import db
from app.models import Plugin, PluginInstallation, PluginLockEntry, User, Workspace
from app.models.notification import NOTIF_PLUGIN_UPDATE, Notification
from app.services.notifications import notify
from app.services.plugin_catalog import (
    CatalogEntry,
    CatalogError,
    CatalogUnavailableError,
    PluginCatalog,
    load_plugin_catalog,
)
from app.services.plugin_compat import enforce_compatible
from app.services.plugin_signatures import (
    VERIFY_IF_PRESENT,
    evaluate_signature,
)
from app.services.plugin_versions import is_newer, latest_version, parse_version
from app.services.plugins import PluginManifest


def _load_catalog(config: Any | None, catalog: PluginCatalog | None) -> PluginCatalog | None:
    """Return the configured catalog, or ``None`` when unavailable/invalid."""
    if catalog is not None:
        return catalog
    source = config
    if source is None and has_app_context():
        source = current_app.config
    try:
        return load_plugin_catalog(source or {})
    except (CatalogUnavailableError, CatalogError):
        return None


def _newer_compatible_entries(
    plugin: Plugin,
    *,
    catalog: PluginCatalog,
) -> list[CatalogEntry]:
    """Return catalog entries newer than ``plugin`` and compatible with the app."""
    trusted_publishers = {}
    if has_app_context():
        trusted_publishers = current_app.config.get("PLUGIN_TRUSTED_KEYS") or {}

    return _compatible_updates(
        plugin,
        catalog.list_entries(),
        trusted_publishers=trusted_publishers,
    )


def _compatible_updates(
    plugin: Plugin,
    entries,
    *,
    trusted_publishers: dict,
) -> list[CatalogEntry]:
    """Filter ``entries`` to newer, compatible versions of ``plugin``.

    Entries for other plugins are ignored, so callers may pass a grouped slice
    of the catalog instead of the whole catalog.
    """
    updates: list[CatalogEntry] = []
    for entry in entries:
        if entry.plugin_id != plugin.id:
            continue
        if not is_newer(entry.version, plugin.version):
            continue
        try:
            manifest = PluginManifest.from_dict(
                entry.manifest,
                trusted_publishers=trusted_publishers,
                trust_policy=VERIFY_IF_PRESENT,
            )
            enforce_compatible(manifest.compatibility, subject=f"Plugin {manifest.id}")
        except Exception:
            # A malformed or incompatible entry is not an "available update".
            continue
        updates.append(entry)
    updates.sort(key=lambda item: parse_version(item.version))
    return updates


def latest_available_update(
    plugin: Plugin,
    *,
    config: Any | None = None,
    catalog: PluginCatalog | None = None,
) -> CatalogEntry | None:
    """Return the newest compatible catalog version newer than ``plugin``."""
    active = _load_catalog(config, catalog)
    if active is None:
        return None
    updates = _newer_compatible_entries(plugin, catalog=active)
    return updates[-1] if updates else None


def available_updates(
    workspace: Workspace | int,
    *,
    config: Any | None = None,
    catalog: PluginCatalog | None = None,
) -> list[dict]:
    """Report installed plugins with a newer, compatible catalog version (#400).

    The catalog is loaded once and grouped by plugin id, so every entry is
    examined once even when the workspace has many installed plugins. Only
    entries that are both newer than the installed version and compatible with
    the running application are returned. Results are ordered by plugin id.
    """
    workspace_obj = (
        db.session.get(Workspace, workspace) if isinstance(workspace, int) else workspace
    )
    if workspace_obj is None:
        return []

    active = _load_catalog(config, catalog)
    if active is None:
        return []

    installed_ids = [
        row.plugin_id
        for row in PluginInstallation.query.filter_by(workspace_id=workspace_obj.id).all()
    ]
    if not installed_ids:
        return []

    entries_by_plugin: dict[str, list[CatalogEntry]] = {}
    for entry in active.list_entries():
        entries_by_plugin.setdefault(entry.plugin_id, []).append(entry)

    trusted_publishers = {}
    if has_app_context():
        trusted_publishers = current_app.config.get("PLUGIN_TRUSTED_KEYS") or {}

    plugins = Plugin.query.filter(Plugin.id.in_(installed_ids)).order_by(Plugin.id).all()
    results: list[dict] = []
    for plugin in plugins:
        candidates = entries_by_plugin.get(plugin.id)
        if not candidates:
            continue
        updates = _compatible_updates(
            plugin,
            candidates,
            trusted_publishers=trusted_publishers,
        )
        if not updates:
            continue
        latest = updates[-1]
        results.append(
            {
                "plugin_id": plugin.id,
                "name": plugin.name,
                "current_version": plugin.version,
                "available_version": latest.version,
                "compatible": True,
                "source": latest.source,
                "integrity": latest.integrity,
            }
        )
    return results


def update_plan(
    plugin: Plugin,
    *,
    workspace_id: int | None = None,
    config: Any | None = None,
    catalog: PluginCatalog | None = None,
) -> dict:
    """Return the current version, available update and rollback target (#403)."""
    active = _load_catalog(config, catalog)
    available = None
    if active is not None:
        latest = latest_available_update(plugin, catalog=active)
        if latest is not None:
            trusted_publishers = {}
            if has_app_context():
                trusted_publishers = current_app.config.get("PLUGIN_TRUSTED_KEYS") or {}
            signature = (
                latest.signature
                if latest.signature is not None
                else latest.manifest.get("signature")
            )
            trust = evaluate_signature(latest.manifest, signature, trusted_publishers)
            available = {
                "version": latest.version,
                "trust_state": trust.state,
                "trust_publisher": trust.publisher,
                "compatible": True,
                "source": latest.source,
                "integrity": latest.integrity,
            }

    return {
        "current_version": plugin.version,
        "available_update": available,
        "rollback_version": rollback_target(
            plugin,
            workspace_id=workspace_id,
            config=config,
            catalog=active,
        ),
    }


def rollback_target(
    plugin: Plugin,
    *,
    workspace_id: int | None = None,
    config: Any | None = None,
    catalog: PluginCatalog | None = None,
) -> str | None:
    """Return the version a rollback would restore, or ``None``.

    The workspace lock entry is preferred when it points at an older version
    (the same rule the rollback API uses). Otherwise the newest catalog version
    older than the installed one is used.
    """
    if workspace_id is not None:
        lock = PluginLockEntry.query.filter_by(
            workspace_id=workspace_id,
            plugin_id=plugin.id,
        ).first()
        if lock is not None and is_newer(plugin.version, lock.version):
            return lock.version

    active = _load_catalog(config, catalog)
    if active is None:
        return None
    older = [
        entry.version
        for entry in active.list_entries()
        if entry.plugin_id == plugin.id and is_newer(plugin.version, entry.version)
    ]
    return latest_version(older)


def _already_notified(owner_id: int, workspace_id: int, plugin_id: str, version: str) -> bool:
    rows = Notification.query.filter_by(
        user_id=owner_id,
        type=NOTIF_PLUGIN_UPDATE,
        workspace_id=workspace_id,
    ).all()
    for row in rows:
        payload = row.payload or {}
        if payload.get("plugin_id") == plugin_id and payload.get("version") == version:
            return True
    return False


def notify_available_updates(
    workspace: Workspace | int,
    *,
    config: Any | None = None,
    catalog: PluginCatalog | None = None,
) -> list[Notification]:
    """Notify the workspace owner once per available plugin version (#404).

    Reuses the Phase 7 notification service (so opt-outs are honoured) and
    dedupes on ``(plugin_id, version)``. Returns the notifications created in
    this call (an empty list when nothing new is available or the owner opted
    out).
    """
    workspace_obj = (
        db.session.get(Workspace, workspace) if isinstance(workspace, int) else workspace
    )
    if workspace_obj is None:
        return []
    owner = db.session.get(User, workspace_obj.user_id)
    if owner is None:
        return []

    active = _load_catalog(config, catalog)
    if active is None:
        return []

    installed_ids = [
        row.plugin_id
        for row in PluginInstallation.query.filter_by(workspace_id=workspace_obj.id).all()
    ]
    if not installed_ids:
        return []
    plugins = Plugin.query.filter(Plugin.id.in_(installed_ids)).order_by(Plugin.id).all()

    created: list[Notification] = []
    for plugin in plugins:
        latest = latest_available_update(plugin, catalog=active)
        if latest is None:
            continue
        if _already_notified(owner.id, workspace_obj.id, plugin.id, latest.version):
            continue
        notification = notify(
            owner,
            NOTIF_PLUGIN_UPDATE,
            workspace=workspace_obj,
            payload={
                "plugin_id": plugin.id,
                "version": latest.version,
                "current_version": plugin.version,
            },
            link=f"/plugins/workspaces/{workspace_obj.id}/plugins/{plugin.id}",
        )
        if notification is not None:
            created.append(notification)

    if created:
        db.session.commit()
    return created


__all__ = [
    "available_updates",
    "latest_available_update",
    "notify_available_updates",
    "rollback_target",
    "update_plan",
]
