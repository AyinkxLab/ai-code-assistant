"""Workspace notification-channel pages and API (#631)."""

from __future__ import annotations

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Workspace
from app.models.notification_channel import CHANNELS, EVENT_TYPES
from app.notification_settings import bp
from app.services import notification_channels
from app.services.permissions import require_workspace_capability, resolve_workspace


def _workspace(workspace_id: int) -> Workspace:
    resolve_workspace(workspace_id)
    return db.session.get(Workspace, workspace_id)


@bp.route("/<int:workspace_id>/notifications")
@login_required
@require_workspace_capability("manage_settings")
def page(workspace_id: int):
    """Render the notification-channel preference matrix."""
    workspace = _workspace(workspace_id)
    return render_template(
        "workspaces/notifications.html",
        workspace=workspace,
        matrix=notification_channels.get_matrix(workspace_id),
        channels=list(CHANNELS),
        event_types=list(EVENT_TYPES),
    )


@bp.route("/api/workspaces/<int:workspace_id>/notification-channels", methods=["GET"])
@login_required
@require_workspace_capability("manage_settings")
def api_get(workspace_id: int):
    """Return the workspace's event-type channel matrix."""
    return jsonify(notification_channels.get_matrix(workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/notification-channels", methods=["PUT"])
@login_required
@require_workspace_capability("manage_settings")
def api_update(workspace_id: int):
    """Validate and persist the channel matrix."""
    data = request.get_json(silent=True) or {}
    try:
        matrix = notification_channels.update_matrix(workspace_id, current_user, data.get("rows"))
    except notification_channels.NotificationChannelError as exc:
        return jsonify({"error": str(exc), "code": "invalid_channel_config"}), 400
    return jsonify(matrix)


@bp.route("/api/workspaces/<int:workspace_id>/notification-channels/test", methods=["POST"])
@login_required
@require_workspace_capability("manage_settings")
def api_test(workspace_id: int):
    """Send a test notification through one channel."""
    workspace = _workspace(workspace_id)
    data = request.get_json(silent=True) or {}
    try:
        result = notification_channels.send_test(
            workspace,
            current_user,
            event_type=(data.get("event_type") or "").strip(),
            channel=(data.get("channel") or "").strip(),
            webhook_url=(data.get("webhook_url") or None),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_test_send"}), 400
    return jsonify(result)
