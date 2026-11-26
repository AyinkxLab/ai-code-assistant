"""CRUD endpoints for safe IdP group-to-role mappings (#593)."""

from __future__ import annotations

from functools import wraps

from flask import jsonify, request
from flask_login import current_user

from app.api.error_catalog import (
    CONFLICT,
    FORBIDDEN,
    INVALID_INPUT,
    NOT_FOUND,
    UNAUTHORIZED,
    error_response,
)
from app.group_roles import bp
from app.services import scim_groups


def _login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        return view(*args, **kwargs)

    return wrapped


def _failure(exc: Exception):
    """Map a service failure to a stable catalog response."""
    if isinstance(exc, scim_groups.ScimNotFound):
        return error_response(NOT_FOUND, str(exc))
    if isinstance(exc, scim_groups.ScimPermissionDenied):
        return error_response(FORBIDDEN, str(exc))
    if isinstance(exc, scim_groups.ScimConflict):
        return error_response(CONFLICT, str(exc))
    return error_response(INVALID_INPUT, str(exc))


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.route("/<int:workspace_id>", methods=["GET"])
@_login_required
def list_group_roles(workspace_id: int):
    """List group role mappings for an organization."""
    try:
        return jsonify({"items": scim_groups.list_mappings(workspace_id, actor=current_user)})
    except Exception as exc:
        return _failure(exc)


@bp.route("/<int:workspace_id>", methods=["POST"])
@_login_required
def create_group_role(workspace_id: int):
    """Create a mapping to viewer or contributor only."""
    data = _body()
    try:
        result = scim_groups.create_mapping(
            workspace_id,
            actor=current_user,
            group_external_id=data.get("group_external_id"),
            role=data.get("role"),
        )
    except Exception as exc:
        return _failure(exc)
    return jsonify(result), 201


@bp.route("/<int:workspace_id>/<int:mapping_id>", methods=["PUT"])
@_login_required
def update_group_role(workspace_id: int, mapping_id: int):
    """Update a mapping and reapply its role to synchronized groups."""
    data = _body()
    try:
        return jsonify(
            scim_groups.update_mapping(
                workspace_id,
                mapping_id,
                actor=current_user,
                role=data.get("role"),
                group_external_id=data.get("group_external_id"),
            )
        )
    except Exception as exc:
        return _failure(exc)


@bp.route("/<int:workspace_id>/<int:mapping_id>", methods=["DELETE"])
@_login_required
def delete_group_role(workspace_id: int, mapping_id: int):
    """Delete a mapping and revoke roles granted through its groups."""
    try:
        scim_groups.delete_mapping(workspace_id, mapping_id, actor=current_user)
    except Exception as exc:
        return _failure(exc)
    return "", 204
