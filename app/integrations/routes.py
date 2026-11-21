"""Owner-scoped integration configuration, health, and inbound email routes.

Configuration and test actions require the ``manage_integrations`` capability,
which only workspace owners hold. The inbound email endpoint is public by design
but authenticates every request with a fresh HMAC signature over the raw body
using the workspace's encrypted ``inbound_secret``.
"""

from __future__ import annotations

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf, db
from app.integrations import bp
from app.models import Workspace
from app.services.integrations import email as email_integration
from app.services.integrations import health
from app.services.integrations.common import (
    IntegrationError,
    IntegrationNotFound,
    IntegrationValidationError,
)
from app.services.permissions import require_workspace_capability

_CREDENTIAL_FIELDS = {
    "slack": ("webhook_url",),
    "teams": ("webhook_url",),
    "email": ("inbound_secret", "reply_to"),
}


def _workspace(workspace_id: int) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise IntegrationNotFound("Workspace not found.")
    return workspace


def _normalize_integration(value) -> str:
    integration = str(value or "").strip().lower()
    if integration not in _CREDENTIAL_FIELDS:
        raise IntegrationValidationError(f"unsupported integration: {integration or '<empty>'}")
    return integration


def _error(payload: IntegrationError):
    return (
        jsonify({"error": str(payload), "code": getattr(payload, "code", "integration_error")}),
        getattr(payload, "http_status", 400),
    )


def _configure(workspace_id: int, integration: str, data: dict) -> dict:
    if integration == "slack":
        from app.services.integrations import slack

        return slack.configure(workspace_id, current_user, data.get("webhook_url"))
    if integration == "teams":
        from app.services.integrations import teams

        return teams.configure(workspace_id, current_user, data.get("webhook_url"))
    return email_integration.configure(
        workspace_id,
        current_user,
        inbound_secret=data.get("inbound_secret"),
        reply_to=data.get("reply_to"),
    )


def _delete(workspace_id: int, integration: str) -> None:
    if integration == "slack":
        from app.services.integrations import slack

        slack.delete_configuration(workspace_id, current_user)
    elif integration == "teams":
        from app.services.integrations import teams

        teams.delete_configuration(workspace_id, current_user)
    else:
        email_integration.delete_configuration(workspace_id, current_user)


@bp.route("/api/workspaces/<int:workspace_id>/integrations", methods=["GET"])
@login_required
@require_workspace_capability("manage_integrations")
def api_list_integrations(workspace_id: int):
    """List every integration's configuration metadata and health."""
    return jsonify(
        {
            "workspace_id": workspace_id,
            "integrations": health.list_integrations(workspace_id),
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/integrations/health", methods=["GET"])
@login_required
@require_workspace_capability("manage_integrations")
def api_list_health(workspace_id: int):
    """Return the non-secret health snapshot for every integration."""
    return jsonify({"workspace_id": workspace_id, "health": health.list_health(workspace_id)})


@bp.route(
    "/api/workspaces/<int:workspace_id>/integrations/<integration>",
    methods=["PUT", "DELETE"],
)
@login_required
@require_workspace_capability("manage_integrations")
def api_configure_integration(workspace_id: int, integration: str):
    """Store or remove one integration's encrypted credentials (owner only)."""
    try:
        integration = _normalize_integration(integration)
        _workspace(workspace_id)
        if request.method == "DELETE":
            _delete(workspace_id, integration)
            return jsonify(
                {
                    "integration": integration,
                    "configured": False,
                    "credential_names": [],
                }
            )
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise IntegrationValidationError("request body must be a JSON object.")
        if integration in ("slack", "teams") and not data.get("webhook_url"):
            raise IntegrationValidationError("webhook_url is required.")
        if integration == "email" and not (data.get("inbound_secret") or data.get("reply_to")):
            raise IntegrationValidationError("Provide inbound_secret and/or reply_to.")
        _configure(workspace_id, integration, data)
        return jsonify(health.get_integration(workspace_id, integration))
    except IntegrationError as exc:
        return _error(exc)


@bp.route(
    "/api/workspaces/<int:workspace_id>/integrations/<integration>/test",
    methods=["POST"],
)
@login_required
@require_workspace_capability("manage_integrations")
def api_test_integration(workspace_id: int, integration: str):
    """Run a manual test-send/test-connection and return the structured result."""
    try:
        integration = _normalize_integration(integration)
        workspace = _workspace(workspace_id)
        result = health.test_connection(workspace, current_user, integration)
    except IntegrationError as exc:
        return _error(exc)
    status_code = 200 if result.get("status") in ("sent", "skipped") else 502
    return jsonify(result), status_code


@bp.route("/email/<int:workspace_id>/inbound", methods=["POST"])
@csrf.exempt
def email_inbound(workspace_id: int):
    """Authenticate and accept a signed inbound email from a mail relay."""
    try:
        result = email_integration.process_inbound(
            workspace_id,
            request.get_data(cache=False),
            request.headers.get("X-Timestamp", ""),
            request.headers.get("X-Signature", ""),
        )
    except IntegrationError as exc:
        return _error(exc)
    return jsonify(result), 202


@bp.route("/email/unsubscribe", methods=["GET", "POST"])
@csrf.exempt
def email_unsubscribe():
    """Suppress a recipient using a signed unsubscribe token."""
    token = request.args.get("token")
    if not token:
        data = request.get_json(silent=True) or {}
        token = data.get("token") if isinstance(data, dict) else None
    try:
        result = email_integration.unsubscribe(token)
    except IntegrationError as exc:
        if request.method == "GET":
            return (
                render_template(
                    "integrations/unsubscribe.html",
                    ok=False,
                    message=str(exc),
                ),
                getattr(exc, "http_status", 400),
            )
        return _error(exc)
    if request.method == "GET":
        return render_template("integrations/unsubscribe.html", ok=True, message=None)
    return jsonify(result)


__all__ = [
    "api_configure_integration",
    "api_list_health",
    "api_list_integrations",
    "api_test_integration",
    "email_inbound",
    "email_unsubscribe",
]
