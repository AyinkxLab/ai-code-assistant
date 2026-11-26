"""SCIM 2.0 Users endpoints (#592).

The blueprint is mounted at ``/scim/v2`` and every route is protected by the
standalone bearer-token guard in :mod:`app.scim.auth`. Responses use RFC 7644
resource and error shapes; they never use the application's public API error
catalog or Flask-Login sessions.
"""

from __future__ import annotations

from flask import g, jsonify, request

from app.scim import bp
from app.scim import users as scim_users
from app.scim.errors import (
    SCIM_TYPE_INVALID_SYNTAX,
    ScimError,
    scim_error,
)

SERVICE_PROVIDER_CONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"


@bp.errorhandler(ScimError)
def _handle_scim_error(error: ScimError):
    return scim_error(error.status, error.detail, error.scim_type)


def _context() -> tuple[int, int]:
    owner_id = getattr(g, "scim_owner_id", None)
    workspace_id = getattr(g, "scim_workspace_id", None)
    if owner_id is None or workspace_id is None:
        raise ScimError(401, "A SCIM bearer token is required.")
    return owner_id, workspace_id


def _json_payload() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ScimError(400, "A JSON request body is required.", SCIM_TYPE_INVALID_SYNTAX)
    return payload


@bp.route("/ServiceProviderConfig", methods=["GET"])
def service_provider_config():
    """Describe the small, supported subset of SCIM 2.0."""
    _context()
    return jsonify(
        {
            "schemas": [SERVICE_PROVIDER_CONFIG_SCHEMA],
            "documentationUri": "/docs/sso-scim-setup.md",
            "patch": {"supported": True},
            "filter": {"supported": True, "maxResults": scim_users.MAX_COUNT},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
        }
    )


@bp.route("/Users", methods=["GET"])
def list_users():
    """List and filter users visible to the provisioning workspace."""
    owner_id, workspace_id = _context()
    result = scim_users.list_users(
        owner_id,
        workspace_id,
        request.args.get("filter"),
        request.args.get("startIndex"),
        request.args.get("count"),
    )
    return jsonify(result)


@bp.route("/Users/<user_id>", methods=["GET"])
def get_user(user_id: str):
    """Return one SCIM User resource."""
    owner_id, workspace_id = _context()
    return jsonify(scim_users.to_scim_user(scim_users.get_user(owner_id, workspace_id, user_id)))


@bp.route("/Users", methods=["POST"])
def create_user():
    """Provision a user and add them to the default workspace."""
    owner_id, workspace_id = _context()
    user = scim_users.create_user(owner_id, workspace_id, _json_payload())
    body = jsonify(scim_users.to_scim_user(user))
    body.headers["Location"] = f"/scim/v2/Users/{user.id}"
    return body, 201


@bp.route("/Users/<user_id>", methods=["PATCH"])
def update_user(user_id: str):
    """Apply a SCIM PATCH operation to a user."""
    owner_id, workspace_id = _context()
    user = scim_users.get_user(owner_id, workspace_id, user_id)
    return jsonify(scim_users.to_scim_user(scim_users.update_user(user, _json_payload())))


@bp.route("/Users/<user_id>", methods=["DELETE"])
def delete_user(user_id: str):
    """Deactivate a user; the account is retained for audit history."""
    owner_id, workspace_id = _context()
    user = scim_users.get_user(owner_id, workspace_id, user_id)
    scim_users.deactivate_user(user)
    return "", 204
