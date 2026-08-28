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

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Plugin, PluginInstallation, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE
from app.plugins import bp
from app.services.capabilities import Capability, CapabilityStore
from app.services.permissions import require_workspace_capability, resolve_workspace, role_for
from app.services.plugins import ManifestValidationError, PluginError, PluginManifest


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
    return {
        "id": plugin.id,
        "name": plugin.name,
        "version": plugin.version,
        "description": plugin.description,
        "author": plugin.author,
        "entry_point": plugin.entry_point,
        "declared_capabilities": plugin.capabilities or [],
        "permissions": plugin.permissions or [],
        "dependencies": plugin.dependencies or [],
        "installed": installation is not None,
        "enabled": bool(installation is not None and installation.enabled),
        "granted_capabilities": granted,
    }


def _capability_name(value: str) -> str | None:
    """Return the Capability enum name for an enum name or value, else None."""
    for cap in Capability:
        if value in (cap.name, cap.value):
            return cap.name
    return None


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    """Plugin management page (workspace-scoped)."""
    workspaces = _accessible_workspaces(current_user)
    return render_template("plugins/index.html", workspaces=workspaces)


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
    resolve_workspace(workspace_id)
    plugins = Plugin.query.order_by(Plugin.id).all()
    return jsonify(
        {
            "workspace": {"id": workspace_id},
            "plugins": [_serialize(p, workspace_id) for p in plugins],
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
        manifest = PluginManifest.from_dict(manifest_data)
    except ManifestValidationError as exc:
        return jsonify({"error": f"Invalid plugin manifest: {exc}"}), 400
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400

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
            configuration=manifest.configuration or {},
        )
        db.session.add(plugin)
    elif plugin.entry_point != manifest.entry_point:
        return jsonify({"error": "Manifest entry_point does not match the registered plugin."}), 409

    if _installation(manifest.id, workspace_id) is not None:
        db.session.rollback()
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

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
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

    return jsonify(_serialize(plugin, workspace_id)), 201


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/enable", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_enable_plugin(workspace_id: int, plugin_id: str):
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    installation.enabled = True
    db.session.commit()
    return jsonify(_serialize(installation.plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/disable", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_disable_plugin(workspace_id: int, plugin_id: str):
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    installation.enabled = False
    db.session.commit()
    return jsonify(_serialize(installation.plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/capabilities", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_update_capabilities(workspace_id: int, plugin_id: str):
    """Explicitly grant/revoke capabilities for a plugin in a workspace.

    Grants are never implicit: each requested capability must be declared in
    the plugin's manifest and is recorded through ``CapabilityStore``.
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
            return jsonify({"error": f"Unknown capability: {raw}"}), 400
        if cap_name not in declared:
            return jsonify({"error": f"Capability {cap_name} is not declared by this plugin."}), 400

    for raw in grant_names:
        cap_name = _capability_name(str(raw))
        if cap_name is not None and cap_name in declared:
            CapabilityStore.grant(plugin_id, workspace_id, Capability[cap_name].value)
    for raw in revoke_names:
        cap_name = _capability_name(str(raw))
        if cap_name is not None and cap_name in declared:
            CapabilityStore.revoke(plugin_id, workspace_id, Capability[cap_name].value)

    db.session.commit()
    return jsonify(_serialize(plugin, workspace_id))
