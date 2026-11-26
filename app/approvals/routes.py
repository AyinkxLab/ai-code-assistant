"""JSON endpoints for the two-person approval workflow (#601)."""

from __future__ import annotations

from functools import wraps

from flask import jsonify, request
from flask_login import current_user

from app.api.error_catalog import (
    CONFLICT,
    FORBIDDEN,
    GONE,
    INVALID_INPUT,
    NOT_FOUND,
    UNAUTHORIZED,
    error_response,
)
from app.approvals import bp
from app.extensions import db
from app.services import approvals


def _login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        return view(*args, **kwargs)

    return wrapped


def _failure(exc: Exception):
    """Map a service failure to a stable catalog response."""
    db.session.rollback()
    if isinstance(exc, approvals.ApprovalNotFound):
        return error_response(NOT_FOUND, str(exc))
    if isinstance(exc, approvals.ApprovalPermissionDenied):
        return error_response(FORBIDDEN, str(exc))
    if isinstance(exc, approvals.ApprovalExpired):
        return error_response(GONE, str(exc))
    if isinstance(exc, approvals.ApprovalConflict):
        return error_response(CONFLICT, str(exc))
    return error_response(INVALID_INPUT, str(exc))


def _json_body() -> dict | None:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


@bp.route("", methods=["POST"])
@_login_required
def create():
    """Open a pending approval request for a sensitive action."""
    data = _json_body()
    if data is None:
        return error_response(INVALID_INPUT, "A JSON object body is required.")
    try:
        result = approvals.create_request(
            data.get("workspace_id"),
            action=data.get("action"),
            target_type=data.get("target_type"),
            target_id=data.get("target_id"),
            payload=data.get("payload"),
            ttl_minutes=data.get("ttl_minutes"),
            actor=current_user,
        )
    except Exception as exc:
        return _failure(exc)
    return jsonify(result), 201


@bp.route("", methods=["GET"])
@_login_required
def list_all():
    """List approval requests for an organization, expiring stale rows first."""
    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id is None:
        return error_response(INVALID_INPUT, "workspace_id is required.")
    try:
        return jsonify({"items": approvals.list_requests(workspace_id, actor=current_user)})
    except Exception as exc:
        return _failure(exc)


@bp.route("/<int:request_id>", methods=["GET"])
@_login_required
def get(request_id: int):
    """Return one approval request."""
    try:
        return jsonify(approvals.get_request(request_id, actor=current_user))
    except Exception as exc:
        return _failure(exc)


@bp.route("/<int:request_id>/approve", methods=["POST"])
@_login_required
def approve(request_id: int):
    """Approve a request from a second organization administrator."""
    try:
        return jsonify(approvals.approve(request_id, actor=current_user))
    except Exception as exc:
        return _failure(exc)


@bp.route("/<int:request_id>/reject", methods=["POST"])
@_login_required
def reject(request_id: int):
    """Reject a request from a second organization administrator."""
    data = _json_body() or {}
    try:
        return jsonify(approvals.reject(request_id, actor=current_user, reason=data.get("reason")))
    except Exception as exc:
        return _failure(exc)


@bp.route("/<int:request_id>/authorize", methods=["POST"])
@_login_required
def authorize(request_id: int):
    """Release a sensitive action only after a distinct approver decided."""
    data = _json_body() or {}
    try:
        return jsonify(
            approvals.authorize(
                request_id,
                actor=current_user,
                action=data.get("action"),
                workspace_id=data.get("workspace_id"),
                target_id=data.get("target_id"),
            )
        )
    except Exception as exc:
        return _failure(exc)
