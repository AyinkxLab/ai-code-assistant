"""Organization role CRUD API (#598).

All routes are organization-scoped through the workspace id and require an
organization owner or admin. Every refusal uses the shared error catalog, and
role mutations delegate validation, built-in protection, and auditing to
:mod:`app.services.organization_roles`.
"""

from __future__ import annotations

import functools

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app.api.error_catalog import (
    CONFLICT,
    FORBIDDEN,
    INVALID_INPUT,
    NOT_FOUND,
    UNAUTHORIZED,
    error_response,
)
from app.extensions import db
from app.services import organization_roles as roles_service
from app.services.permissions import resolve_workspace

bp = Blueprint("organization_roles", __name__)

_STATUS_TO_CATALOG = {
    400: INVALID_INPUT,
    403: FORBIDDEN,
    404: NOT_FOUND,
    409: CONFLICT,
}


def _login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        return view(*args, **kwargs)

    return wrapper


def _service_error(exc: roles_service.OrganizationRoleError):
    db.session.rollback()
    details = {"reason": exc.code, **exc.details}
    return error_response(
        _STATUS_TO_CATALOG.get(exc.status, INVALID_INPUT),
        exc.message,
        details=details,
    )


def _json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


@bp.route("/api/workspaces/<int:workspace_id>/roles", methods=["GET", "POST"])
@_login_required
def collection(workspace_id: int):
    """List organization roles or create a custom one."""
    resolve_workspace(workspace_id)
    try:
        if request.method == "GET":
            return jsonify({"roles": roles_service.list_roles(workspace_id, actor=current_user)})
        data = _json_object()
        if data is None:
            return error_response(INVALID_INPUT, "A JSON object is required.")
        return (
            jsonify(roles_service.create_role(workspace_id, data, actor=current_user)),
            201,
        )
    except roles_service.OrganizationRoleError as exc:
        return _service_error(exc)


@bp.route(
    "/api/workspaces/<int:workspace_id>/roles/<int:role_id>", methods=["GET", "PUT", "DELETE"]
)
@_login_required
def detail(workspace_id: int, role_id: int):
    """Read, update, or delete one custom organization role."""
    resolve_workspace(workspace_id)
    try:
        if request.method == "GET":
            return jsonify(roles_service.get_role(workspace_id, role_id, actor=current_user))
        if request.method == "PUT":
            data = _json_object()
            if data is None:
                return error_response(INVALID_INPUT, "A JSON object is required.")
            return jsonify(
                roles_service.update_role(workspace_id, role_id, data, actor=current_user)
            )
        return jsonify(roles_service.delete_role(workspace_id, role_id, actor=current_user))
    except roles_service.OrganizationRoleError as exc:
        return _service_error(exc)
