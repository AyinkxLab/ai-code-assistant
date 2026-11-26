"""Minimal SCIM 2.0 group endpoints (#593).

Only ``/scim/v2/Groups`` is implemented; there is deliberately no Users
endpoint, JIT provisioning, or OIDC/SAML relying party. Requests authenticate
with an existing scoped API token or an organization admin browser session.
"""

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
from app.scim import bp
from app.services import scim_groups

LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


def _login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        return view(*args, **kwargs)

    return wrapped


def _failure(exc: Exception):
    """Map a SCIM service failure to a stable catalog response."""
    if isinstance(exc, scim_groups.ScimNotFound):
        return error_response(NOT_FOUND, str(exc))
    if isinstance(exc, scim_groups.ScimPermissionDenied):
        return error_response(FORBIDDEN, str(exc))
    if isinstance(exc, scim_groups.ScimConflict):
        return error_response(CONFLICT, str(exc))
    return error_response(INVALID_INPUT, str(exc))


def _body() -> dict:
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def _workspace_id():
    value = request.args.get("workspace_id", type=int)
    if value is None:
        return None, error_response(INVALID_INPUT, "workspace_id is required.")
    return value, None


@bp.route("/Groups", methods=["GET"])
@_login_required
def list_groups():
    """Return the groups belonging to one organization."""
    workspace_id, failure = _workspace_id()
    if failure is not None:
        return failure
    try:
        resources = scim_groups.list_groups(workspace_id, actor=current_user)
    except Exception as exc:
        return _failure(exc)
    return jsonify(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": len(resources),
            "Resources": resources,
        }
    )


@bp.route("/Groups", methods=["POST"])
@_login_required
def create_group():
    """Create one directory group from a SCIM-shaped JSON body."""
    workspace_id, failure = _workspace_id()
    if failure is not None:
        return failure
    try:
        result = scim_groups.create_group(workspace_id, actor=current_user, payload=_body())
    except Exception as exc:
        return _failure(exc)
    return jsonify(result), 201


@bp.route("/Groups/<int:group_id>", methods=["GET"])
@_login_required
def get_group(group_id: int):
    """Return one directory group."""
    try:
        return jsonify(scim_groups.get_group(group_id, actor=current_user))
    except Exception as exc:
        return _failure(exc)


@bp.route("/Groups/<int:group_id>", methods=["PUT"])
@_login_required
def replace_group(group_id: int):
    """Replace one directory group and its complete member list."""
    try:
        return jsonify(scim_groups.replace_group(group_id, actor=current_user, payload=_body()))
    except Exception as exc:
        return _failure(exc)


@bp.route("/Groups/<int:group_id>", methods=["PATCH"])
@_login_required
def patch_group(group_id: int):
    """Apply a minimal SCIM patch to one directory group."""
    try:
        return jsonify(scim_groups.patch_group(group_id, actor=current_user, payload=_body()))
    except Exception as exc:
        return _failure(exc)


@bp.route("/Groups/<int:group_id>", methods=["DELETE"])
@_login_required
def delete_group(group_id: int):
    """Delete one directory group and revoke the roles it granted."""
    try:
        scim_groups.delete_group(group_id, actor=current_user)
    except Exception as exc:
        return _failure(exc)
    return "", 204
