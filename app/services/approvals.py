"""Two-person approval workflow for sensitive actions (#601).

The workflow is additive: it does not widen or weaken any existing workspace
capability. A requester opens an ``ApprovalRequest`` for a registered sensitive
action; a *different* organization administrator approves or rejects it. The
``authorize`` helper refuses to release an action until that second decision
exists, so callers can gate role changes and data deletions without changing
the legacy endpoints in place.

No token material is created here. The bounded JSON payload is validated so
credential-shaped keys can never be persisted or echoed to clients.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.extensions import db
from app.models import ApprovalRequest, Workspace
from app.models.approval_request import (
    ACTION_DATA_DELETION,
    ACTION_ROLE_CHANGE,
    MAX_REASON_LENGTH,
    MAX_TARGET_TYPE_LENGTH,
    SENSITIVE_ACTIONS,
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from app.services import audit
from app.services.organizations import organization_role

DEFAULT_TTL_MINUTES = 60
MAX_TTL_MINUTES = 10080  # one week
MAX_PAYLOAD_BYTES = 4096

#: Keys that must never be stored in an approval payload or audit row.
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|authorization|cookie|session|otp|mfa)",
    re.IGNORECASE,
)


class ApprovalError(ValueError):
    """The request input is invalid."""


class ApprovalPermissionDenied(PermissionError):
    """The caller may not request, decide, or use this approval."""


class ApprovalNotFound(LookupError):
    """The workspace or approval request does not exist."""


class ApprovalConflict(Exception):
    """The request is in a state that forbids the attempted transition."""


class ApprovalExpired(ApprovalConflict):
    """The request window has closed."""


def _moment(value: datetime | None) -> datetime:
    return value or datetime.now(UTC)


def _naive(value: datetime) -> datetime:
    """SQLite stores naive UTC values; compare against naive bounds."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _as_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _require_admin(workspace_id: int, actor) -> None:
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise ApprovalPermissionDenied("Authentication is required.")
    if organization_role(workspace_id, actor) is None:
        # A global ``is_admin`` flag is deliberately ignored: built-in admin is
        # not an implicit member of every organization.
        raise ApprovalPermissionDenied("Organization admin access is required.")


def validate_action(value) -> str:
    """Return a registered sensitive action name or raise :class:`ApprovalError`."""
    if not isinstance(value, str) or value.strip() not in SENSITIVE_ACTIONS:
        raise ApprovalError("action must be role_change or data_deletion.")
    return value.strip()


def validate_target_type(value) -> str:
    """Return a bounded target type string or raise :class:`ApprovalError`."""
    if not isinstance(value, str) or not value.strip():
        raise ApprovalError("target_type is required.")
    normalized = value.strip()
    if len(normalized) > MAX_TARGET_TYPE_LENGTH:
        raise ApprovalError(f"target_type must be at most {MAX_TARGET_TYPE_LENGTH} characters.")
    return normalized


def validate_target_id(value) -> int | None:
    """Return a positive target id (or ``None``) without accepting booleans."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApprovalError("target_id must be a positive integer.")
    return value


def validate_ttl(value) -> int:
    """Return a bounded minute count for a request's lifetime."""
    if value is None or value == "":
        return DEFAULT_TTL_MINUTES
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApprovalError("ttl_minutes must be an integer.")
    if not 1 <= value <= MAX_TTL_MINUTES:
        raise ApprovalError(f"ttl_minutes must be between 1 and {MAX_TTL_MINUTES}.")
    return value


def validate_payload(value) -> dict:
    """Validate and return a JSON-serializable, secret-free payload object."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApprovalError("payload must be a JSON object.")

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or _SECRET_KEY_RE.search(key):
                    raise ApprovalError("payload must not contain credential-like keys.")
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)
        elif not isinstance(node, (str, int, float, bool)) and node is not None:
            raise ApprovalError("payload must contain only plain JSON values.")

    _walk(value)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ApprovalError("payload must be JSON serializable.") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ApprovalError(f"payload must be at most {MAX_PAYLOAD_BYTES} bytes.")
    return value


def validate_reason(value) -> str | None:
    """Return a bounded decision reason or raise :class:`ApprovalError`."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApprovalError("reason must be a string.")
    normalized = value.strip()
    if len(normalized) > MAX_REASON_LENGTH:
        raise ApprovalError(f"reason must be at most {MAX_REASON_LENGTH} characters.")
    return normalized or None


def _workspace(workspace_id) -> Workspace:
    if isinstance(workspace_id, bool) or not isinstance(workspace_id, int) or workspace_id < 1:
        raise ApprovalError("workspace_id must be a positive integer.")
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise ApprovalNotFound("Workspace not found.")
    return workspace


def _get_request(request_id) -> ApprovalRequest:
    if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
        raise ApprovalNotFound("Approval request not found.")
    row = db.session.get(ApprovalRequest, request_id)
    if row is None:
        raise ApprovalNotFound("Approval request not found.")
    return row


def expire_stale(*, now: datetime | None = None, commit: bool = True) -> int:
    """Mark every pending request past its deadline as expired and audit it."""
    moment = _moment(now)
    stale = (
        ApprovalRequest.query.filter_by(status=STATUS_PENDING)
        .filter(ApprovalRequest.expires_at <= _naive(moment))
        .all()
    )
    for row in stale:
        row.status = STATUS_EXPIRED
        row.decided_at = moment
        row.decision_reason = "expired"
        audit.record(
            audit.APPROVAL_EXPIRED,
            workspace_id=row.workspace_id,
            user_id=row.requester_id,
            target_type="approval_request",
            target_id=row.id,
            metadata={"action": row.action, "status": STATUS_EXPIRED},
        )
    if commit and stale:
        db.session.commit()
    return len(stale)


def create_request(
    workspace_id,
    *,
    action,
    target_type,
    actor,
    target_id=None,
    payload=None,
    ttl_minutes=None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Open a pending approval request and audit the submission."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    moment = _moment(now)
    row = ApprovalRequest(
        workspace_id=workspace_id,
        action=validate_action(action),
        target_type=validate_target_type(target_type),
        target_id=validate_target_id(target_id),
        payload=json.dumps(validate_payload(payload), sort_keys=True, separators=(",", ":")),
        status=STATUS_PENDING,
        requester_id=actor.id,
        created_at=moment,
        expires_at=moment + timedelta(minutes=validate_ttl(ttl_minutes)),
    )
    duplicate = ApprovalRequest.query.filter_by(
        workspace_id=workspace_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        status=STATUS_PENDING,
    ).first()
    if duplicate is not None:
        raise ApprovalConflict("An equivalent approval request is already pending.")
    db.session.add(row)
    db.session.flush()
    audit.record(
        audit.APPROVAL_REQUESTED,
        workspace_id=workspace_id,
        user=actor,
        target_type="approval_request",
        target_id=row.id,
        metadata={"action": row.action, "target_type": row.target_type, "target_id": row.target_id},
    )
    if commit:
        db.session.commit()
    return row.to_dict()


def _decide(request_id, actor, *, status: str, reason, now: datetime | None) -> ApprovalRequest:
    expire_stale(commit=False)
    row = _get_request(request_id)
    _require_admin(row.workspace_id, actor)
    if actor.id == row.requester_id:
        raise ApprovalPermissionDenied("A second approver is required.")
    if row.status != STATUS_PENDING:
        if row.status == STATUS_EXPIRED:
            raise ApprovalExpired("This approval request has expired.")
        raise ApprovalConflict("This approval request has already been decided.")
    normalized_reason = validate_reason(reason)
    moment = _moment(now)
    row.status = status
    row.decided_at = moment
    row.decided_by_id = actor.id
    row.decision_reason = normalized_reason
    action = audit.APPROVAL_APPROVED if status == STATUS_APPROVED else audit.APPROVAL_REJECTED
    audit.record(
        action,
        workspace_id=row.workspace_id,
        user=actor,
        target_type="approval_request",
        target_id=row.id,
        metadata={
            "action": row.action,
            "decision": status,
            "requester_id": row.requester_id,
            "reason": normalized_reason or "",
        },
    )
    db.session.commit()
    return row


def approve(request_id, *, actor, now: datetime | None = None) -> dict:
    """Approve a pending request from a second organization administrator."""
    return _decide(request_id, actor, status=STATUS_APPROVED, reason=None, now=now).to_dict()


def reject(request_id, *, actor, reason=None, now: datetime | None = None) -> dict:
    """Reject a pending request from a second organization administrator."""
    row = _decide(request_id, actor, status=STATUS_REJECTED, reason=reason, now=now)
    return row.to_dict()


def list_requests(workspace_id, *, actor) -> list[dict]:
    """List requests for one organization, expiring stale rows first."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    expire_stale()
    rows = (
        ApprovalRequest.query.filter_by(workspace_id=workspace_id)
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        .all()
    )
    return [row.to_dict() for row in rows]


def get_request(request_id, *, actor) -> dict:
    """Return one request, expiring it first when its window has closed."""
    expire_stale()
    row = _get_request(request_id)
    _require_admin(row.workspace_id, actor)
    return row.to_dict()


def authorize(
    request_id,
    *,
    actor,
    action=None,
    workspace_id=None,
    target_id=None,
) -> dict:
    """Release a sensitive action only after a distinct administrator approved it.

    The caller must be the requester. Passing ``action``/``workspace_id``/
    ``target_id`` binds the approval to the exact operation being attempted so
    an approval for one target cannot authorize another.
    """
    expire_stale(commit=False)
    row = _get_request(request_id)
    _require_admin(row.workspace_id, actor)
    if actor.id != row.requester_id:
        raise ApprovalPermissionDenied("Only the requester may use an approval.")
    if action is not None and validate_action(action) != row.action:
        raise ApprovalConflict("The approval is for a different action.")
    if workspace_id is not None and workspace_id != row.workspace_id:
        raise ApprovalConflict("The approval is for a different workspace.")
    if target_id is not None and target_id != row.target_id:
        raise ApprovalConflict("The approval is for a different target.")
    if row.status == STATUS_PENDING:
        raise ApprovalConflict("A second approver has not decided this request.")
    if row.status == STATUS_REJECTED:
        raise ApprovalPermissionDenied("This approval request was rejected.")
    if row.status == STATUS_EXPIRED:
        raise ApprovalExpired("This approval request has expired.")
    if row.decided_by_id is None or row.decided_by_id == row.requester_id:
        raise ApprovalConflict("The approval must come from a second administrator.")
    db.session.commit()
    return row.to_dict()


__all__ = [
    "ACTION_DATA_DELETION",
    "ACTION_ROLE_CHANGE",
    "DEFAULT_TTL_MINUTES",
    "MAX_PAYLOAD_BYTES",
    "MAX_TTL_MINUTES",
    "SENSITIVE_ACTIONS",
    "ApprovalConflict",
    "ApprovalError",
    "ApprovalExpired",
    "ApprovalNotFound",
    "ApprovalPermissionDenied",
    "approve",
    "authorize",
    "create_request",
    "expire_stale",
    "get_request",
    "list_requests",
    "validate_action",
    "validate_payload",
    "validate_reason",
    "validate_target_id",
    "validate_target_type",
    "validate_ttl",
]
