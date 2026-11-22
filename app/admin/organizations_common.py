"""Shared helpers for the Phase 11 organization admin console (#626).

Routes live in ``organization_dashboard``, ``organization_members`` and
``organization_usage``; this module holds the authorization and response
helpers they share so there is one place that decides whether a caller may
administer an organization.
"""

from __future__ import annotations

from flask import jsonify, redirect, request, url_for
from flask_login import current_user

from app.api.error_catalog import FORBIDDEN, NOT_FOUND, error_response
from app.extensions import db
from app.models import Workspace
from app.models.workspace_member import MEMBER_ROLES
from app.services import organizations

#: Roles the console may assign. ``admin`` is an organization role; it is not
#: part of the workspace capability matrix.
ORG_ASSIGNABLE_ROLES = (*MEMBER_ROLES, organizations.ORG_ROLE_ADMIN)


def wants_json() -> bool:
    """Return ``True`` when the caller expects a JSON response."""
    return (
        request.is_json
        or request.args.get("format") == "json"
        or request.accept_mimetypes.best == "application/json"
    )


def confirmed() -> bool:
    """Return ``True`` when the caller checked the confirmation box."""
    return (request.values.get("confirm") or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_organization(workspace_id: int):
    """Resolve the workspace and require an owner/admin caller.

    Returns ``(workspace, error)``; ``error`` is a structured response when the
    caller may not administer the organization, otherwise ``None``.
    """
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        return None, error_response(NOT_FOUND, "Organization not found.")
    if organizations.organization_role(workspace_id, current_user) is None:
        return None, error_response(FORBIDDEN, "Organization admin access required.")
    return workspace, None


def action_payload() -> dict:
    """Return form or JSON input as one dict."""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict()


def success(message: str, *, payload=None, status: int = 200):
    """Return JSON or redirect back to the member console with a flash."""
    if wants_json():
        return jsonify(payload if payload is not None else {"ok": True}), status
    from flask import flash

    flash(message, "success")
    return redirect(
        url_for("admin.organization_members", workspace_id=request.view_args["workspace_id"]),
        code=303,
    )
