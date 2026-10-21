"""Plugin management routes (Phase 8 #163).

Workspace-scoped plugin management:
- list accessible workspaces
- list registered plugins with per-workspace installation state
- inspect a plugin
- install a trusted/local plugin manifest (validated, identity-bound)
- enable / disable a workspace installation
- grant / revoke plugin capabilities explicitly

Authorization reuses the Phase 7 model: any workspace member may view;
only the workspace owner may install, enable, disable, or grant capabilities.
The backend is authoritative and never trusts the UI.
"""

import json

from flask import abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    Plugin,
    PluginInstallation,
    PluginLockEntry,
    Workspace,
    WorkspaceMember,
)
from app.models.workspace_member import STATUS_ACTIVE
from app.plugins import bp
from app.services.capabilities import Capability, CapabilityStore
from app.services.events import emit_event
from app.services.marketplace import (
    MarketplaceEntryNotFoundError,
    MarketplaceError,
    filter_entries,
    load_marketplace_catalog,
    marketplace_error_payload,
)
from app.services.permissions import (
    can,
    require_workspace_capability,
    resolve_workspace,
    role_for,
)
from app.services.plugin_audit import record_plugin_audit
from app.services.plugin_catalog import (
    CatalogEntryNotFoundError,
    CatalogError,
    CatalogUnavailableError,
    filter_catalog_entries,
    load_plugin_catalog,
)
from app.services.plugin_compat import (
    IncompatibleCompatibilityError,
    InvalidCompatibilityError,
    compatibility_status,
    enforce_compatible,
)
from app.services.plugin_config import validate_plugin_config
from app.services.plugin_errors import list_workspace_error_reports
from app.services.plugin_ops import (
    AtomicUpdateError,
    PluginEntryPointMismatchError,
    PluginNotFoundError,
    PluginUpdateError,
    RemoteInstallError,
    atomic_rollback,
    atomic_update_from_catalog,
    discover_plugins,
    rollback_from_catalog,
    update_plugin,
    validate_catalog_install,
)
from app.services.plugin_signatures import (
    VERIFY_IF_PRESENT,
    PublisherSignatureError,
    enforce_publisher_trust,
    evaluate_signature,
)
from app.services.plugin_trust import (
    PluginTrustConfigError,
    trusted_publisher_ids,
)
from app.services.plugin_updates import notify_available_updates, update_plan
from app.services.plugin_versions import is_newer, latest_version
from app.services.plugins import ManifestValidationError, PluginError, PluginManifest

#: Hard maximum number of plugins returned in a single marketplace page (#379).
MAX_PAGE_LIMIT = 100
#: Default page size for the marketplace listing.
DEFAULT_PAGE_LIMIT = 50


class PluginAlreadyInstalledError(ValueError):
    """Raised when a plugin is already installed in the workspace."""


def _accessible_workspaces(user):
    """Return the workspaces ``user`` owns or is an active member of."""
    seen: dict[int, Workspace] = {}
    for ws in Workspace.query.filter_by(user_id=user.id).all():
        seen[ws.id] = ws
    memberships = WorkspaceMember.query.filter_by(user_id=user.id, status=STATUS_ACTIVE).all()
    for membership in memberships:
        ws = db.session.get(Workspace, membership.workspace_id)
        if ws is not None:
            seen[ws.id] = ws
    return sorted(seen.values(), key=lambda w: w.id)


def _installation(plugin_id: str, workspace_id: int) -> PluginInstallation | None:
    return PluginInstallation.query.filter_by(
        plugin_id=plugin_id,
        workspace_id=workspace_id,
    ).first()


def _serialize(plugin: Plugin, workspace_id: int) -> dict:
    """Serialize a plugin with its installation state for ``workspace_id``.

    ``config`` is deliberately not included: it may hold secrets and is not
    needed for the management surface.
    """
    installation = _installation(plugin.id, workspace_id)
    if installation is not None:
        granted = CapabilityStore.list_capabilities(plugin.id, workspace_id)
    else:
        granted = []
    status = compatibility_status(plugin.compatibility)
    role = role_for(workspace_id, current_user)
    can_manage = can("manage_plugins", workspace_id)

    # A catalog install records the artifact digest in the workspace lockfile.
    # Local-manifest installs have no artifact, so the hash is simply absent
    # rather than invented.
    lock_entry = PluginLockEntry.query.filter_by(
        workspace_id=workspace_id,
        plugin_id=plugin.id,
    ).first()
    artifact_hash = lock_entry.integrity if lock_entry is not None else None
    source = lock_entry.source if lock_entry is not None else None
    installed = installation is not None
    enabled = bool(installed and installation.enabled)

    return {
        "id": plugin.id,
        "workspace_id": workspace_id,
        "role": role,
        "can_manage": can_manage,
        "name": plugin.name,
        "version": plugin.version,
        "description": plugin.description,
        "author": plugin.author,
        "entry_point": plugin.entry_point,
        "declared_capabilities": plugin.capabilities or [],
        "permissions": plugin.permissions or [],
        "dependencies": plugin.dependencies or [],
        "compatibility": plugin.compatibility or "any",
        "trust_state": plugin.trust_state,
        "trust_publisher": plugin.trust_publisher,
        "compatible_with_app": bool(status["compatible"]),
        "app_version": status["app_version"],
        "installed": installed,
        "enabled": enabled,
        "granted_capabilities": granted,
        "artifact_hash": artifact_hash,
        "artifact_sha256": artifact_hash,
        "integrity": artifact_hash,
        "source": source,
        "can_install": bool(can_manage and not installed),
        "can_update": bool(can_manage and installed),
        "can_enable": bool(can_manage and installed and not enabled),
        "can_disable": bool(can_manage and installed and enabled),
        "can_uninstall": bool(can_manage and installed),
        "actions": {
            "install": bool(can_manage and not installed),
            "update": bool(can_manage and installed),
            "enable": bool(can_manage and installed and not enabled),
            "disable": bool(can_manage and installed and enabled),
            "uninstall": bool(can_manage and installed),
        },
    }


def _catalog_entry_state(
    entry,
    workspace_id: int,
    *,
    versions_by_id: dict[str, list],
) -> dict:
    """Serialize one catalog entry with per-workspace install/update state.

    Only safe metadata is exposed: no configuration values, artifact bytes or
    key material. Trust is evaluated without raising so a forged signature is
    shown as ``Invalid`` instead of breaking the whole browse response.
    """
    manifest = entry.manifest or {}
    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, str):
        compatibility = None
    status = compatibility_status(compatibility)
    trusted_publishers = current_app.config.get("PLUGIN_TRUSTED_KEYS") or {}
    signature = entry.signature if entry.signature is not None else manifest.get("signature")
    trust = evaluate_signature(manifest, signature, trusted_publishers)
    publisher_key_id = trust.publisher or entry.publisher

    installed_plugin = db.session.get(Plugin, entry.plugin_id)
    installation = _installation(entry.plugin_id, workspace_id)
    installed = installation is not None and installed_plugin is not None
    installed_version = installed_plugin.version if installed else None
    enabled = bool(installed and installation.enabled)

    all_versions = versions_by_id.get(entry.plugin_id, [])
    latest = all_versions[-1].version if all_versions else entry.version
    newer = [
        candidate.version
        for candidate in all_versions
        if installed and is_newer(candidate.version, installed_version)
    ]
    older = [
        candidate.version
        for candidate in all_versions
        if installed and is_newer(installed_version, candidate.version)
    ]

    rollback_version = None
    if installed:
        lock_entry = PluginLockEntry.query.filter_by(
            workspace_id=workspace_id,
            plugin_id=entry.plugin_id,
        ).first()
        if lock_entry is not None and is_newer(installed_version, lock_entry.version):
            rollback_version = lock_entry.version
        else:
            rollback_version = latest_version(older)

    can_manage = can("manage_plugins", workspace_id)
    update_version = latest_version(newer)
    dependencies = list(manifest.get("dependencies") or [])
    return {
        "id": entry.plugin_id,
        "plugin_id": entry.plugin_id,
        "workspace_id": workspace_id,
        "name": manifest.get("name") or entry.plugin_id,
        "version": entry.version,
        "latest_version": latest,
        "is_latest": entry.version == latest,
        "description": manifest.get("description") or "",
        "author": manifest.get("author") or "",
        "capabilities": list(manifest.get("capabilities") or []),
        "permissions": list(manifest.get("permissions") or []),
        "dependencies": dependencies,
        "dependency_count": len(dependencies),
        "compatibility": compatibility or "any",
        "compatible_with_app": bool(status["compatible"]),
        "app_version": status["app_version"],
        "trust_state": trust.state,
        "trust_publisher": trust.publisher,
        "publisher_key_id": publisher_key_id,
        "signature_present": bool(trust.signature_present),
        "source": entry.source,
        "integrity": entry.integrity,
        "artifact_sha256": entry.integrity,
        "artifact": {
            "url": entry.source,
            "sha256": entry.integrity,
            "publisher_key_id": publisher_key_id,
        },
        "installed": installed,
        "installed_version": installed_version,
        "enabled": enabled,
        "update_available": bool(newer),
        "update_version": update_version,
        "rollback_available": rollback_version is not None,
        "rollback_version": rollback_version,
        "role": role_for(workspace_id, current_user),
        "can_manage": can_manage,
        "actions": {
            "install": bool(can_manage and not installed),
            "update": bool(can_manage and installed and update_version is not None),
            "rollback": bool(can_manage and installed and rollback_version is not None),
        },
    }


def _manifest_payload(plugin: Plugin) -> str:
    """Return an editable manifest JSON document for the update form."""
    data = {
        "id": plugin.id,
        "name": plugin.name,
        "version": plugin.version,
        "description": plugin.description or "",
        "author": plugin.author or "",
        "entry_point": plugin.entry_point,
        "capabilities": plugin.capabilities or [],
        "permissions": plugin.permissions or [],
        "dependencies": plugin.dependencies or [],
        "compatibility": plugin.compatibility,
        "configuration": plugin.configuration or {},
    }
    if not data["compatibility"]:
        data.pop("compatibility")
    return json.dumps(data, indent=2, sort_keys=True)


def _capability_name(value: str) -> str | None:
    """Return the Capability enum name for an enum name or value, else None."""
    for cap in Capability:
        if value in (cap.name, cap.value):
            return cap.name
    return None


def _upsert_plugin(manifest: PluginManifest) -> Plugin:
    """Create or reuse the global ``Plugin`` row for a validated manifest.

    The manifest id is the identity, and an existing plugin keeps its entry
    point: a conflicting entry point raises
    :class:`PluginEntryPointMismatchError` so an install/update can never
    retarget registered code.
    """
    plugin = Plugin.query.filter_by(id=manifest.id).first()
    if plugin is None:
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
        return plugin

    if plugin.entry_point != manifest.entry_point:
        raise PluginEntryPointMismatchError(
            "Manifest entry_point does not match the registered plugin."
        )

    # Trust metadata is derived from the verified signature, so refresh it even
    # when the global row already exists (for example after a key rotation).
    plugin.trust_state = manifest.trust_state
    plugin.trust_publisher = manifest.trust_publisher
    return plugin


def _record_catalog_lock(
    plugin: Plugin,
    workspace_id: int,
    *,
    version: str,
    source: str,
    integrity: str,
    dependencies,
) -> PluginLockEntry:
    """Persist the catalog artifact digest for the detail page and lockfiles."""
    row = PluginLockEntry.query.filter_by(
        workspace_id=workspace_id,
        plugin_id=plugin.id,
    ).first()
    if row is None:
        row = PluginLockEntry(
            workspace_id=workspace_id,
            plugin_id=plugin.id,
            owner_id=current_user.id,
        )
        db.session.add(row)
    row.version = version
    row.source = source
    row.integrity = integrity
    row.dependencies = list(dependencies or [])
    db.session.commit()
    return row


def _install_in_workspace(manifest: PluginManifest, workspace_id: int) -> Plugin:
    """Register ``manifest`` and install it in ``workspace_id``.

    Shared by the local-manifest and catalog install routes so both paths keep
    the same identity binding and duplicate-install handling. Neither path
    creates capability grants. Returns the persisted plugin.

    Raises:
        PluginAlreadyInstalledError: If the plugin is already installed in the
            workspace (or a concurrent request won the unique constraint).
    """
    plugin = _upsert_plugin(manifest)
    if _installation(manifest.id, workspace_id) is not None:
        db.session.rollback()
        raise PluginAlreadyInstalledError

    installation = PluginInstallation(
        plugin_id=manifest.id,
        workspace_id=workspace_id,
        enabled=True,
        config=manifest.configuration or {},
        installed_by_id=current_user.id,
    )
    db.session.add(installation)
    try:
        db.session.commit()
    except Exception:  # uniqueness races map to a duplicate install
        db.session.rollback()
        raise PluginAlreadyInstalledError from None
    return plugin


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    """Plugin management page (workspace-scoped)."""
    workspaces = _accessible_workspaces(current_user)
    return render_template("plugins/index.html", workspaces=workspaces)


@bp.route("/workspaces/<int:workspace_id>/plugins/<plugin_id>")
@bp.route("/workspaces/<int:workspace_id>/<plugin_id>")
@bp.route("/plugins/<int:workspace_id>/<plugin_id>")
@login_required
def detail(workspace_id: int, plugin_id: str):
    """Render the plugin detail page (#417).

    Any workspace member may view metadata and read-only state. Install,
    update, and lifecycle controls are rendered only for the workspace owner;
    the API enforces the same rule independently of the UI.
    """
    workspace = resolve_workspace(workspace_id)
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        abort(404)
    plan = update_plan(plugin, workspace_id=workspace_id)
    return render_template(
        "plugins/detail.html",
        workspace=workspace,
        plugin=_serialize(plugin, workspace_id),
        update_manifest=_manifest_payload(plugin),
        update_plan=plan,
    )


# --------------------------------------------------------------------------
# API: accessible workspaces
# --------------------------------------------------------------------------


@bp.route("/api/workspaces")
@login_required
def api_list_workspaces():
    workspaces = []
    for ws in _accessible_workspaces(current_user):
        workspaces.append(
            {
                "id": ws.id,
                "name": ws.name,
                "role": role_for(ws.id, current_user),
            }
        )
    return jsonify(workspaces)


# --------------------------------------------------------------------------
# API: list and inspect plugins (viewable by any workspace member)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins", methods=["GET"])
@login_required
def api_list_plugins(workspace_id: int):
    """List registered plugins with stable, bounded pagination (#379).

    Query params: ``page`` (1-based) and ``limit``. ``limit`` is clamped to
    :data:`MAX_PAGE_LIMIT`, so an oversized request never returns an unbounded
    page. Ordering is by plugin id, making pages stable across requests.
    """
    resolve_workspace(workspace_id)

    page = request.args.get("page", type=int)
    if page is None or page < 1:
        page = 1
    limit = request.args.get("limit", type=int)
    if limit is None:
        limit = DEFAULT_PAGE_LIMIT
    limit = max(1, min(limit, MAX_PAGE_LIMIT))

    total = Plugin.query.count()
    pages = (total + limit - 1) // limit if total else 0
    offset = (page - 1) * limit
    rows = Plugin.query.order_by(Plugin.id).offset(offset).limit(limit).all()

    return jsonify(
        {
            "workspace": {"id": workspace_id},
            "plugins": [_serialize(p, workspace_id) for p in rows],
            "pagination": {
                "page": page,
                "limit": limit,
                "max_limit": MAX_PAGE_LIMIT,
                "total": total,
                "pages": pages,
                "has_next": page < pages,
                "has_prev": page > 1,
            },
            "ordering": ["id"],
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/catalog", methods=["GET"])
@bp.route("/api/workspaces/<int:workspace_id>/marketplace", methods=["GET"])
@login_required
def api_browse_catalog(workspace_id: int):
    """Browse the marketplace catalog (readable by any workspace member).

    Returns catalog entries with trust, compatibility, dependency metadata,
    artifact/source integrity and per-workspace install/update/rollback state.
    Pagination uses the same bounded, deterministic convention as the plugin
    list (page >= 1, limit clamped to :data:`MAX_PAGE_LIMIT`, sorted by plugin
    id then version). Members are read-only: ``actions`` is only true when the
    caller holds ``manage_plugins``, and every mutating endpoint independently
    enforces that role.
    """
    workspace = resolve_workspace(workspace_id)
    try:
        catalog = load_plugin_catalog(current_app.config)
    except CatalogUnavailableError as exc:
        return jsonify({"error": str(exc), "code": "catalog_unavailable"}), 404
    except CatalogError as exc:
        return jsonify({"error": f"Invalid plugin catalog: {exc}"}), 400

    page = request.args.get("page", type=int)
    if page is None or page < 1:
        page = 1
    limit = request.args.get("limit", type=int)
    if limit is None:
        limit = DEFAULT_PAGE_LIMIT
    limit = max(1, min(limit, MAX_PAGE_LIMIT))

    versions_by_id: dict[str, list] = {}
    for entry in catalog.list_entries():
        versions_by_id.setdefault(entry.plugin_id, []).append(entry)

    all_entries = filter_catalog_entries(
        list(catalog.list_entries()),
        query=request.args.get("q"),
        capability=request.args.get("capability"),
        tag=request.args.get("tag"),
    )
    total = len(all_entries)
    pages = (total + limit - 1) // limit if total else 0
    offset = (page - 1) * limit
    page_entries = all_entries[offset : offset + limit]
    plugins = [
        _catalog_entry_state(entry, workspace_id, versions_by_id=versions_by_id)
        for entry in page_entries
    ]
    return jsonify(
        {
            "workspace": {
                "id": workspace.id,
                "role": role_for(workspace_id, current_user),
            },
            "plugins": plugins,
            "entries": plugins,
            "pagination": {
                "page": page,
                "limit": limit,
                "max_limit": MAX_PAGE_LIMIT,
                "total": total,
                "pages": pages,
                "has_next": page < pages,
                "has_prev": page > 1,
            },
            "ordering": ["id", "version"],
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>", methods=["GET"])
@login_required
def api_inspect_plugin(workspace_id: int, plugin_id: str):
    resolve_workspace(workspace_id)
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        return jsonify({"error": "Plugin not found."}), 404
    return jsonify(_serialize(plugin, workspace_id))


# --------------------------------------------------------------------------
# API: trusted publisher key management (read-only, #386)
# --------------------------------------------------------------------------


@bp.route("/api/trusted-publishers", methods=["GET"])
@login_required
def api_list_trusted_publishers():
    """List the configured trusted publisher ids (never key material).

    The configured keys live in ``PLUGIN_TRUSTED_KEYS`` (an operator secret
    setting). This read-only surface lets an operator confirm *which* publisher
    ids are trusted and under which policy, without exposing any key bytes or
    fingerprints. Invalid key material is rejected when the application loads
    (``app.services.plugin_trust.validate_trust_config``).
    """
    publishers = trusted_publisher_ids(current_app.config.get("PLUGIN_TRUSTED_KEYS") or {})
    policy = current_app.config.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT)
    return jsonify(
        {
            "publishers": publishers,
            "count": len(publishers),
            "policy": policy,
        }
    )


# --------------------------------------------------------------------------
# API: plugin discovery (read-only, local-directory based)
# --------------------------------------------------------------------------


@bp.route("/discover", methods=["GET"])
@login_required
def api_discover_plugins():
    """List plugins found in the configured discovery directory (#172).

    Read-only: returns the id/name/version/description/capabilities of each
    manifest discovered in ``PLUGIN_DISCOVERY_DIR`` that is not already
    registered, without installing or loading anything.
    """
    return jsonify({"plugins": discover_plugins()})


# --------------------------------------------------------------------------
# API: install / enable / disable / capabilities (owner only)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins/install", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_install_plugin(workspace_id: int):
    """Install a trusted/local plugin manifest into the workspace.

    The plugin id is taken solely from the validated manifest (identity
    binding); a client cannot override it. Installation creates or reuses the
    global ``Plugin`` row and a per-workspace ``PluginInstallation``. It never
    loads or executes plugin code and never creates capability grants.
    """
    data = request.get_json(silent=True) or {}
    manifest_data = data.get("manifest")
    if not isinstance(manifest_data, dict):
        return jsonify({"error": "A manifest object is required."}), 400

    try:
        manifest = PluginManifest.from_dict(
            manifest_data,
            trusted_publishers=current_app.config["PLUGIN_TRUSTED_KEYS"],
            trust_policy=current_app.config["PLUGIN_TRUST_POLICY"],
        )
    except ManifestValidationError as exc:
        return jsonify({"error": f"Invalid plugin manifest: {exc}"}), 400
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400

    # Reject a plugin whose explicit PEP 440 compatibility range does not
    # include the running application version (an absent field is unrestricted).
    try:
        enforce_compatible(manifest.compatibility, subject=f"Plugin {manifest.id}")
    except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        plugin = _install_in_workspace(manifest, workspace_id)
    except PluginEntryPointMismatchError:
        db.session.rollback()
        return jsonify({"error": "Manifest entry_point does not match the registered plugin."}), 409
    except PluginAlreadyInstalledError:
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

    return jsonify(_serialize(plugin, workspace_id)), 201


@bp.route(
    "/api/workspaces/<int:workspace_id>/plugins/install-from-catalog",
    methods=["POST"],
)
@login_required
@require_workspace_capability("manage_plugins")
def api_install_plugin_from_catalog(workspace_id: int):
    """Install a plugin pinned by the operator-controlled catalog (#389).

    Owner-only. The request names a catalog entry (``plugin_id`` plus an
    optional ``version``); a client cannot supply a manifest, an artifact URL,
    or a publisher key. The entry is validated against the catalog, the
    manifest is re-validated against the manifest schema, and the active
    ``PLUGIN_TRUST_POLICY`` is enforced *before* anything is written:

    - unsigned entries are refused when the policy is ``required``;
    - a forged, malformed, or untrusted signature is always refused;
    - the artifact sha256 from the catalog is recorded in the response.

    Success registers metadata only: no artifact is downloaded, no plugin code
    is executed, and **no capability grant is created**. Grants remain explicit
    per workspace.
    """
    data = request.get_json(silent=True) or {}
    plugin_id = data.get("plugin_id")
    version = data.get("version")

    if not isinstance(plugin_id, str) or not plugin_id.strip():
        return jsonify({"error": "plugin_id is required."}), 400
    if version is not None and (not isinstance(version, str) or not version.strip()):
        return jsonify({"error": "version must be a non-empty string when provided."}), 400

    try:
        catalog = load_plugin_catalog(current_app.config)
    except CatalogUnavailableError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=plugin_id.strip() if isinstance(plugin_id, str) else None,
            outcome="denied",
            reason="catalog unavailable",
        )
        return jsonify({"error": str(exc)}), 404
    except CatalogError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=plugin_id.strip() if isinstance(plugin_id, str) else None,
            outcome="denied",
            reason="invalid catalog",
        )
        return jsonify({"error": f"Invalid plugin catalog: {exc}"}), 400

    try:
        entry = catalog.get(
            plugin_id.strip(),
            version.strip() if version is not None else None,
        )
    except CatalogEntryNotFoundError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=plugin_id.strip(),
            outcome="denied",
            reason="catalog entry not found",
        )
        return jsonify({"error": str(exc)}), 404
    except CatalogError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=plugin_id.strip(),
            outcome="denied",
            reason="invalid catalog version",
        )
        return jsonify({"error": f"Invalid plugin version: {exc}"}), 400

    try:
        validate_catalog_install(catalog, entry)
    except RemoteInstallError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="dependency validation failed",
        )
        return jsonify({"error": str(exc)}), 400

    trusted_publishers = current_app.config.get("PLUGIN_TRUSTED_KEYS") or {}
    try:
        # Schema validation is trust-policy agnostic here: the detached (or
        # embedded) signature is enforced separately below so a catalog entry
        # signed out-of-band is not rejected as "unsigned".
        manifest = PluginManifest.from_dict(
            entry.manifest,
            trusted_publishers=trusted_publishers,
            trust_policy=VERIFY_IF_PRESENT,
        )
    except ManifestValidationError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="invalid manifest",
        )
        return jsonify({"error": f"Invalid plugin manifest: {exc}"}), 400
    except PluginError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="plugin error",
        )
        return jsonify({"error": str(exc)}), 400

    policy = current_app.config.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT)
    # A catalog entry may carry the publisher signature detached (catalog
    # field) or embedded in the manifest; both sign the same canonical body, so
    # the effective signature is whichever was supplied.
    signature = entry.signature if entry.signature is not None else entry.manifest.get("signature")
    try:
        trust = enforce_publisher_trust(
            entry.manifest,
            signature,
            trusted_publishers=trusted_publishers,
            policy=policy,
        )
    except PluginTrustConfigError as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="trust config error",
        )
        return jsonify({"error": str(exc)}), 400
    except PublisherSignatureError as exc:
        # Refusals are auditable but never store artifact bytes or key material.
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="publisher signature refused",
        )
        db.session.commit()
        return jsonify({"error": str(exc)}), 403

    try:
        enforce_compatible(manifest.compatibility, subject=f"Plugin {manifest.id}")
    except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="incompatible compatibility",
        )
        return jsonify({"error": str(exc)}), 400

    # The verified trust fact is what gets persisted, not whatever the catalog
    # (or manifest) claimed.
    manifest.trust_state = trust.state
    manifest.trust_publisher = trust.publisher

    try:
        plugin = _install_in_workspace(manifest, workspace_id)
    except PluginEntryPointMismatchError:
        db.session.rollback()
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="entry point mismatch",
        )
        return jsonify({"error": "Manifest entry_point does not match the registered plugin."}), 409
    except PluginAlreadyInstalledError:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="already installed",
        )
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

    _record_catalog_lock(
        plugin,
        workspace_id,
        version=entry.version,
        source=entry.source,
        integrity=entry.integrity,
        dependencies=manifest.dependencies or [],
    )
    # Audit successful remote install attempt
    record_plugin_audit(
        workspace_id,
        "granted",
        actor=current_user,
        plugin_id=plugin.id,
        capability="remote_install",
        outcome="success",
        reason="installed from catalog",
    )
    payload = _serialize(plugin, workspace_id)
    payload["source"] = entry.source
    payload["integrity"] = entry.integrity
    return jsonify(payload), 201


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/install", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_install_registered_plugin(workspace_id: int, plugin_id: str):
    """Install an already-registered plugin into a workspace (owner only).

    The detail page uses this for installs of plugins that are globally
    registered but not installed here. It records metadata only: no plugin code
    is loaded or executed and no capability grant is created.
    """
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        return jsonify({"error": "Plugin not found."}), 404
    if _installation(plugin_id, workspace_id) is not None:
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

    try:
        enforce_compatible(plugin.compatibility, subject=f"Plugin {plugin.id}")
    except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
        return jsonify({"error": str(exc)}), 400

    installation = PluginInstallation(
        plugin_id=plugin.id,
        workspace_id=workspace_id,
        enabled=True,
        config=plugin.configuration or {},
        installed_by_id=current_user.id,
    )
    db.session.add(installation)
    try:
        db.session.commit()
    except Exception:  # uniqueness races map to a duplicate install
        db.session.rollback()
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

    return jsonify(_serialize(plugin, workspace_id)), 201


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/update", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_update_plugin(workspace_id: int, plugin_id: str):
    """Update a registered plugin from a validated manifest (owner only).

    The manifest is fully validated and identity-bound: its ``id`` must match
    the plugin being updated and its ``entry_point`` must not change, so an
    update can never point an installed plugin at different code. The declared
    PEP 440 ``compatibility`` range is enforced before anything is written; an
    update that excludes the running application version is refused with
    ``400`` and a message naming both the range and the current version. The
    registered plugin (and every workspace installation) is left untouched.

    Capabilities and per-workspace configuration are not modified by an update;
    grants stay explicit and workspace-scoped.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404

    data = request.get_json(silent=True) or {}
    manifest_data = data.get("manifest")
    if not isinstance(manifest_data, dict):
        return jsonify({"error": "A manifest object is required."}), 400

    try:
        manifest = PluginManifest.from_dict(
            manifest_data,
            trusted_publishers=current_app.config["PLUGIN_TRUSTED_KEYS"],
            trust_policy=current_app.config["PLUGIN_TRUST_POLICY"],
        )
    except ManifestValidationError as exc:
        return jsonify({"error": f"Invalid plugin manifest: {exc}"}), 400
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        plugin = update_plugin(plugin_id, manifest)
    except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except PluginEntryPointMismatchError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409
    except PluginUpdateError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except PluginNotFoundError:
        db.session.rollback()
        return jsonify({"error": "Plugin not found."}), 404

    return jsonify(_serialize(plugin, workspace_id))


# --------------------------------------------------------------------------
# API: available updates, atomic catalog update, update notifications
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/update-plan", methods=["GET"])
@login_required
def api_update_plan(workspace_id: int, plugin_id: str):
    """Return the current version, available update and rollback target (#403)."""
    resolve_workspace(workspace_id)
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        return jsonify({"error": "Plugin not found."}), 404
    return jsonify(update_plan(plugin, workspace_id=workspace_id))


@bp.route(
    "/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/update-from-catalog",
    methods=["POST"],
)
@login_required
@require_workspace_capability("manage_plugins")
def api_update_from_catalog(workspace_id: int, plugin_id: str):
    """Atomically update an installed plugin to a catalog version (#401/#403).

    The manifest, publisher signature, artifact integrity and compatibility are
    verified before the plugin row (and its lock entry) are swapped in a single
    transaction. A failure leaves the current version active.
    """
    if _installation(plugin_id, workspace_id) is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404

    data = request.get_json(silent=True) or {}
    version = data.get("version")
    if version is not None and (not isinstance(version, str) or not version.strip()):
        return jsonify({"error": "version must be a non-empty string when provided."}), 400

    try:
        plugin, entry = atomic_update_from_catalog(
            plugin_id,
            version.strip() if version is not None else None,
            workspace_id=workspace_id,
        )
    except CatalogEntryNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except CatalogUnavailableError as exc:
        return jsonify({"error": str(exc)}), 404
    except CatalogError as exc:
        return jsonify({"error": str(exc)}), 400
    except (PublisherSignatureError, PluginTrustConfigError) as exc:
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=plugin_id,
            outcome="denied",
            reason="update refused by trust policy",
        )
        db.session.commit()
        return jsonify({"error": str(exc)}), 403
    except PluginEntryPointMismatchError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409
    except (
        AtomicUpdateError,
        PluginUpdateError,
        PluginNotFoundError,
        ManifestValidationError,
        IncompatibleCompatibilityError,
        InvalidCompatibilityError,
        PluginError,
    ) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    record_plugin_audit(
        workspace_id,
        "granted",
        actor=current_user,
        plugin_id=plugin_id,
        capability="update",
        outcome="success",
        reason=f"updated to {entry.version}",
    )
    db.session.commit()
    emit_event(
        "plugin.updated",
        data={"plugin_id": plugin_id, "version": entry.version},
        workspace_id=workspace_id,
        user_id=current_user.id,
    )
    return jsonify(_serialize(plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/check-updates", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_check_plugin_updates(workspace_id: int):
    """Notify the workspace owner once per newly available plugin version (#404)."""
    workspace = resolve_workspace(workspace_id)
    created = notify_available_updates(workspace)
    return jsonify(
        {
            "created": len(created),
            "notifications": [notification.to_dict() for notification in created],
        }
    )


# --------------------------------------------------------------------------
# API: plugin rollback (owner only)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/rollback", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_rollback_plugin(workspace_id: int, plugin_id: str):
    """Rollback a plugin to its previous version.

    The workspace lock entry is used when it points at an older version;
    otherwise the newest catalog version older than the installed one is used.
    Both paths apply atomically and leave the current version active on failure.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404

    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        return jsonify({"error": "Plugin not found."}), 404
    current_version = plugin.version

    lock_entry = PluginLockEntry.query.filter_by(
        workspace_id=workspace_id,
        plugin_id=plugin_id,
    ).first()
    if lock_entry is None and not (
        current_app.config.get("PLUGIN_CATALOG_FILE")
        or current_app.config.get("PLUGIN_MARKETPLACE_URL")
    ):
        return jsonify({"error": "No lockfile history or catalog is available for rollback."}), 404

    try:
        if lock_entry is not None and is_newer(current_version, lock_entry.version):
            manifest = PluginManifest(
                id=plugin.id,
                name=plugin.name,
                version=lock_entry.version,
                description=plugin.description,
                author=plugin.author,
                entry_point=plugin.entry_point,
                capabilities=plugin.capabilities or [],
                permissions=plugin.permissions or [],
                dependencies=plugin.dependencies or [],
                compatibility=plugin.compatibility,
                configuration=plugin.configuration or {},
                trust_state=plugin.trust_state,
                trust_publisher=plugin.trust_publisher,
            )
            plugin = atomic_rollback(plugin_id, manifest, workspace_id=workspace_id)
            target_version = manifest.version
        else:
            plugin, entry = rollback_from_catalog(plugin_id, workspace_id=workspace_id)
            target_version = entry.version
    except (AtomicUpdateError, PluginUpdateError) as exc:
        db.session.rollback()
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=plugin_id,
            outcome="denied",
            reason=f"rollback failed: {exc!s}",
        )
        return jsonify({"error": str(exc)}), 400
    except (CatalogUnavailableError, CatalogError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404

    record_plugin_audit(
        workspace_id,
        "granted",
        actor=current_user,
        plugin_id=plugin_id,
        capability="rollback",
        outcome="success",
        reason=f"rolled back from {current_version} to {target_version}",
    )
    db.session.commit()
    emit_event(
        "plugin.rolled_back",
        data={"plugin_id": plugin_id, "version": target_version},
        workspace_id=workspace_id,
        user_id=current_user.id,
    )
    return jsonify(_serialize(plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/enable", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_enable_plugin(workspace_id: int, plugin_id: str):
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    if not installation.enabled:
        # Resolve declared dependencies before enabling. The workspace
        # installation must not become enabled when a required dependency is
        # missing, disabled, incompatible, or cyclic.
        from app.services.plugin_ops import (
            PluginDependencyResolutionError,
            resolve_plugin_dependencies,
        )

        try:
            resolve_plugin_dependencies(installation.plugin)
        except PluginDependencyResolutionError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
        installation.enabled = True
        record_plugin_audit(
            workspace_id,
            "enabled",
            actor=current_user,
            plugin_id=plugin_id,
            outcome="success",
        )
        db.session.commit()
        emit_event(
            "plugin.enabled",
            data={"plugin_id": plugin_id},
            workspace_id=workspace_id,
            user_id=current_user.id,
        )
    else:
        db.session.commit()
    return jsonify(_serialize(installation.plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/disable", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_disable_plugin(workspace_id: int, plugin_id: str):
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    if installation.enabled:
        installation.enabled = False
        record_plugin_audit(
            workspace_id,
            "disabled",
            actor=current_user,
            plugin_id=plugin_id,
            outcome="success",
        )
        db.session.commit()
        emit_event(
            "plugin.disabled",
            data={"plugin_id": plugin_id},
            workspace_id=workspace_id,
            user_id=current_user.id,
        )
    else:
        db.session.commit()
    return jsonify(_serialize(installation.plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/uninstall", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_uninstall_plugin(workspace_id: int, plugin_id: str):
    """Uninstall a plugin from the workspace.

    Revokes every capability grant for that workspace, removes the per-workspace
    installation, and emits ``plugin.uninstalled``. The global ``Plugin`` row is
    kept so other workspaces' installations remain intact. Lifecycle code, when
    a plugin runs it, reacts to the ``plugin.uninstalled`` event; the management
    API never loads or executes plugin code itself.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    CapabilityStore.revoke_all(plugin_id, workspace_id)
    db.session.delete(installation)
    db.session.commit()
    emit_event(
        "plugin.uninstalled",
        data={"plugin_id": plugin_id},
        workspace_id=workspace_id,
        user_id=current_user.id,
    )
    return jsonify({"ok": True, "plugin_id": plugin_id})


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/capabilities", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_update_capabilities(workspace_id: int, plugin_id: str):
    """Explicitly grant/revoke capabilities for a plugin in a workspace.

    Grants are never implicit: each requested capability must be declared in
    the plugin's manifest and is recorded through ``CapabilityStore``. Every
    successful grant/revoke and every rejected capability request is appended
    to the workspace audit trail (owner-visible only).
    """
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        return jsonify({"error": "Plugin not found."}), 404
    if _installation(plugin_id, workspace_id) is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404

    data = request.get_json(silent=True) or {}
    declared = set(plugin.capabilities or [])
    grant_names = data.get("grant") or []
    revoke_names = data.get("revoke") or []

    if not isinstance(grant_names, list) or not isinstance(revoke_names, list):
        return jsonify({"error": "grant and revoke must be lists."}), 400

    for raw in [*grant_names, *revoke_names]:
        cap_name = _capability_name(str(raw))
        if cap_name is None:
            record_plugin_audit(
                workspace_id,
                "denied",
                actor=current_user,
                plugin_id=plugin_id,
                capability=str(raw)[:64],
                outcome="denied",
                reason="capability request rejected",
            )
            db.session.commit()
            return jsonify({"error": f"Unknown capability: {raw}"}), 400
        if cap_name not in declared:
            record_plugin_audit(
                workspace_id,
                "denied",
                actor=current_user,
                plugin_id=plugin_id,
                capability=Capability[cap_name].value,
                outcome="denied",
                reason="capability not declared by plugin",
            )
            db.session.commit()
            return jsonify({"error": f"Capability {cap_name} is not declared by this plugin."}), 400

    for raw in grant_names:
        cap_name = _capability_name(str(raw))
        if cap_name is None or cap_name not in declared:
            continue
        value = Capability[cap_name].value
        if CapabilityStore.has_capability(plugin_id, workspace_id, value):
            continue
        CapabilityStore.grant(plugin_id, workspace_id, value, granted_by_id=current_user.id)
        record_plugin_audit(
            workspace_id,
            "granted",
            actor=current_user,
            plugin_id=plugin_id,
            capability=value,
            outcome="success",
        )
    for raw in revoke_names:
        cap_name = _capability_name(str(raw))
        if cap_name is None or cap_name not in declared:
            continue
        value = Capability[cap_name].value
        if CapabilityStore.revoke(plugin_id, workspace_id, value):
            record_plugin_audit(
                workspace_id,
                "revoked",
                actor=current_user,
                plugin_id=plugin_id,
                capability=value,
                outcome="success",
            )

    db.session.commit()
    return jsonify(_serialize(plugin, workspace_id))


# --------------------------------------------------------------------------
# API: per-workspace plugin configuration (owner only)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/config", methods=["GET"])
@login_required
@require_workspace_capability("manage_plugins")
def api_get_plugin_config(workspace_id: int, plugin_id: str):
    """Read a plugin installation's workspace-scoped configuration.

    Owner-only: the config may hold secrets, so it is never exposed on the
    list/inspect surfaces (which omit ``config``) and never to non-owners.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    return jsonify(
        {
            "plugin_id": plugin_id,
            "workspace_id": workspace_id,
            "config": installation.config or {},
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/config", methods=["PUT"])
@login_required
@require_workspace_capability("manage_plugins")
def api_update_plugin_config(workspace_id: int, plugin_id: str):
    """Replace a plugin installation's workspace-scoped configuration.

    The replacement is validated against the manifest's declared ``configuration``
    when one is present (unknown keys and wrong-typed values are rejected with
    a 400); it is size/depth-bounded and never includes secrets from elsewhere.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    plugin = db.session.get(Plugin, plugin_id)

    data = request.get_json(silent=True) or {}
    proposed = data.get("config")
    if proposed is None:
        proposed = {}
    declared = (plugin.configuration if plugin is not None else None) or {}
    ok, reason = validate_plugin_config(declared, proposed)
    if not ok:
        return jsonify({"error": reason}), 400

    installation.config = proposed
    db.session.commit()
    return jsonify(
        {
            "plugin_id": plugin_id,
            "workspace_id": workspace_id,
            "config": installation.config or {},
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/plugin-errors", methods=["GET"])
@login_required
@require_workspace_capability("manage_plugins")
def api_list_plugin_errors(workspace_id: int):
    """Owner-scoped read of a workspace's structured plugin error reports.

    Reports are bounded (no stack traces or payloads) and only ever returned
    for the caller's own workspace (fail closed for non-owners).
    """
    reports = list_workspace_error_reports(workspace_id)
    return jsonify({"workspace_id": workspace_id, "count": len(reports), "errors": reports})


# --------------------------------------------------------------------------
# API: plugin lockfile (owner only)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/lockfile", methods=["GET"])
@login_required
def api_get_lockfile(workspace_id: int):
    """Return the resolved plugin lockfile for a workspace.

    Readable by any workspace member (read-only); mutating plugin management
    still requires the owner. The lockfile lists every plugin pinned for the
    workspace, including the resolved version, artifact source, integrity hash,
    and declared dependencies. It never exposes secrets or artifact contents.
    """
    from app.services.plugin_lockfile import build_lockfile

    resolve_workspace(workspace_id)
    lock_entries = PluginLockEntry.query.filter_by(workspace_id=workspace_id).all()
    lockfile = build_lockfile(lock_entries)
    return jsonify(lockfile)


# --------------------------------------------------------------------------
# API: read-only marketplace catalog (Phase 9, issue #360)
# --------------------------------------------------------------------------


def _marketplace_workspace() -> dict | None:
    """Resolve an optional ``workspace_id`` filter for marketplace reads.

    Without ``workspace_id`` the catalog is global metadata available to any
    signed-in user. When supplied, the caller must be an active member (or the
    owner); non-members get the same uniform 404 as every other workspace read.
    """
    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id is None:
        return None
    workspace = resolve_workspace(workspace_id)
    return {"id": workspace.id, "role": role_for(workspace.id, current_user)}


def _marketplace_error(exc: MarketplaceError):
    """Map typed catalog errors to UI-safe responses (never leak file paths)."""
    return jsonify(marketplace_error_payload(exc)), exc.status


def _marketplace_page(entries):
    """Paginate filtered entries with the shared bounded convention."""
    page = request.args.get("page", type=int)
    if page is None or page < 1:
        page = 1
    limit = request.args.get("limit", type=int)
    if limit is None:
        limit = DEFAULT_PAGE_LIMIT
    limit = max(1, min(limit, MAX_PAGE_LIMIT))

    total = len(entries)
    pages = (total + limit - 1) // limit if total else 0
    offset = (page - 1) * limit
    return entries[offset : offset + limit], {
        "page": page,
        "limit": limit,
        "max_limit": MAX_PAGE_LIMIT,
        "total": total,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
    }


@bp.route("/api/marketplace", methods=["GET"])
@login_required
def api_marketplace():
    """List advertised catalog entries (read-only; any signed-in workspace member).

    Query parameters:
        ``q``             free-text match on id, name, description, author.
        ``capability``    only advertise entries requiring this capability.
        ``workspace_id``  optionally resolve the caller's role in a workspace.
        ``page`` / ``limit`` bounded pagination (limit capped at 100).

    The catalog is untrusted metadata. It is never executed and never grants a
    capability; installation still requires an explicit, validated install.
    """
    try:
        workspace = _marketplace_workspace()
        loaded = load_marketplace_catalog(current_app.config)
    except MarketplaceError as exc:
        return _marketplace_error(exc)
    catalog = loaded.catalog

    query = (request.args.get("q") or "").strip()
    capability = (request.args.get("capability") or "").strip()
    filtered = filter_entries(catalog, query=query, capability=capability)
    page_entries, pagination = _marketplace_page(filtered)
    plugins = [entry.to_dict() for entry in page_entries]
    return jsonify(
        {
            "workspace": workspace,
            "state": loaded.state,
            "stale": loaded.stale,
            "unavailable": loaded.unavailable,
            "entries": plugins,
            "plugins": plugins,
            "pagination": pagination,
            "filters": {"q": query or None, "capability": capability or None},
            "warnings": list(loaded.warnings),
            "ordering": ["id", "version"],
        }
    )


@bp.route("/api/marketplace/<plugin_id>", methods=["GET"])
@login_required
def api_marketplace_entry(plugin_id: str):
    """Return one advertised entry, optionally pinned to ``?version=``."""
    try:
        workspace = _marketplace_workspace()
        loaded = load_marketplace_catalog(current_app.config)
        entry = loaded.catalog.get(plugin_id, request.args.get("version"))
    except MarketplaceEntryNotFoundError as exc:
        return jsonify({"error": str(exc), "code": "not_found"}), 404
    except MarketplaceError as exc:
        return _marketplace_error(exc)

    return jsonify(
        {
            "workspace": workspace,
            "state": loaded.state,
            "stale": loaded.stale,
            "unavailable": loaded.unavailable,
            "entry": entry.to_dict(),
            "warnings": list(loaded.warnings),
        }
    )
