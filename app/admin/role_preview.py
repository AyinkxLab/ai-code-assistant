"""Read-only role and capability preview for the organization console (#602)."""

from __future__ import annotations

from flask import jsonify, render_template, request
from flask_login import login_required

from app.admin import bp
from app.admin.organizations_common import resolve_organization, wants_json
from app.api.error_catalog import INVALID_INPUT, NOT_FOUND, error_response
from app.extensions import db
from app.models import User
from app.services import organizations
from app.services.role_simulation import SIMULATABLE_ROLES, simulate_capabilities


@bp.route("/organizations/<int:workspace_id>/roles/preview")
@login_required
def role_preview(workspace_id: int):
    """Show a user's effective capabilities without changing assignments.

    Organization owners/admins may pass ``user_id`` and an optional hypothetical
    ``role``. The role is used only to calculate the preview; no membership row
    is written. HTML renders the provenance table, while ``format=json`` returns
    the same payload for tooling.
    """
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error

    role_override = (request.args.get("role") or "").strip().lower() or None
    if role_override is not None and role_override not in SIMULATABLE_ROLES:
        return error_response(INVALID_INPUT, "Invalid role.")

    raw_user_id = request.args.get("user_id")
    selected_user_id = None
    payload = None
    if raw_user_id not in (None, ""):
        try:
            selected_user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return error_response(INVALID_INPUT, "user_id must be an integer.")
        target_user = db.session.get(User, selected_user_id)
        if target_user is None:
            return error_response(NOT_FOUND, "User not found.")
        payload = simulate_capabilities(workspace, target_user, role_override)

    if wants_json():
        if payload is None:
            return error_response(INVALID_INPUT, "user_id is required.")
        return jsonify(payload)

    return render_template(
        "admin/role_preview.html",
        workspace=workspace,
        members=organizations.organization_members(workspace),
        roles=SIMULATABLE_ROLES,
        payload=payload,
        selected_user_id=selected_user_id,
        selected_role=role_override,
    )
