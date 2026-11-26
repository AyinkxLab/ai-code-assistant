"""CRUD and re-authentication endpoints for organization session policy (#591)."""

from __future__ import annotations

from functools import wraps

from flask import jsonify, request
from flask_login import current_user

from app.api.error_catalog import (
    FORBIDDEN,
    INVALID_INPUT,
    NOT_FOUND,
    UNAUTHORIZED,
    error_response,
)
from app.services import session_policy
from app.session_policy import bp


def _login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        return view(*args, **kwargs)

    return wrapped


def _failure(exc: Exception):
    """Map a policy service failure to a stable catalog response."""
    if isinstance(exc, session_policy.SessionPolicyNotFound):
        return error_response(NOT_FOUND, str(exc))
    if isinstance(exc, session_policy.SessionPolicyPermissionDenied):
        return error_response(FORBIDDEN, str(exc))
    if isinstance(exc, session_policy.ReauthFailed):
        return error_response(UNAUTHORIZED, str(exc))
    return error_response(INVALID_INPUT, str(exc))


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.route("/<int:workspace_id>", methods=["GET"])
@_login_required
def get_policy(workspace_id: int):
    """Return the organization's session policy, if one is configured."""
    try:
        return jsonify({"policy": session_policy.get_policy(workspace_id, actor=current_user)})
    except Exception as exc:
        return _failure(exc)


@bp.route("/<int:workspace_id>", methods=["PUT"])
@_login_required
def put_policy(workspace_id: int):
    """Create or replace the organization's opt-in session policy."""
    data = _body()
    try:
        result = session_policy.upsert_policy(
            workspace_id,
            actor=current_user,
            max_session_age_minutes=data.get("max_session_age_minutes"),
            idle_timeout_minutes=data.get("idle_timeout_minutes"),
            require_reauth_for_sensitive_actions=bool(
                data.get("require_reauth_for_sensitive_actions", False)
            ),
            reauth_window_minutes=data.get(
                "reauth_window_minutes", session_policy.DEFAULT_REAUTH_WINDOW_MINUTES
            ),
        )
    except Exception as exc:
        return _failure(exc)
    return jsonify(result)


@bp.route("/<int:workspace_id>", methods=["DELETE"])
@_login_required
def delete_policy(workspace_id: int):
    """Remove the policy and restore the existing session behaviour."""
    try:
        session_policy.delete_policy(workspace_id, actor=current_user)
    except Exception as exc:
        return _failure(exc)
    return "", 204


@bp.route("/<int:workspace_id>/reauth", methods=["POST"])
@_login_required
def reauthenticate(workspace_id: int):
    """Refresh the session's re-authentication marker with a password check."""
    try:
        result = session_policy.reauthenticate(
            workspace_id, actor=current_user, password=_body().get("password")
        )
    except Exception as exc:
        return _failure(exc)
    return jsonify(result)
