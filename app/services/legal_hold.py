"""Legal hold placement, release, and enforcement (#609).

A hold is scoped to one workspace and protects either its audit log, its
workspace data, or both (``all``). Every destructive path asks this module
before touching data: audit purge, workspace deletion, and user data deletion
all fail closed while a matching hold is active.

Only the workspace owner (``manage_settings``) may place or release a hold.
Every placement and release appends an :class:`~app.models.AuditLog` row so the
preservation decision is itself auditable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db
from app.models import LegalHold, Workspace, WorkspaceMember
from app.models.legal_hold import (
    MAX_REASON_LENGTH,
    SCOPE_ALL,
    SCOPE_AUDIT,
    SCOPE_WORKSPACE,
    SCOPES,
)
from app.models.workspace_member import STATUS_ACTIVE
from app.services import audit
from app.services.permissions import can


class LegalHoldError(ValueError):
    """Invalid hold input."""


class LegalHoldConflict(ValueError):
    """The hold is already released, so the operation cannot apply."""


class LegalHoldActive(Exception):
    """A destructive operation was refused because a hold is active."""


def validate_scope(value) -> str:
    """Return a canonical scope string or raise :class:`LegalHoldError`."""
    if not isinstance(value, str):
        raise LegalHoldError("scope must be audit, workspace, or all.")
    normalized = value.strip().lower()
    if normalized not in SCOPES:
        raise LegalHoldError("scope must be audit, workspace, or all.")
    return normalized


def validate_reason(value, *, field: str = "reason") -> str:
    """Return a trimmed non-empty reason or raise :class:`LegalHoldError`."""
    if not isinstance(value, str):
        raise LegalHoldError(f"{field} is required.")
    normalized = value.strip()
    if not normalized:
        raise LegalHoldError(f"{field} is required.")
    if len(normalized) > MAX_REASON_LENGTH:
        raise LegalHoldError(f"{field} must be at most {MAX_REASON_LENGTH} characters.")
    return normalized


def _workspace(workspace_id: int) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise LookupError("Workspace not found.")
    return workspace


def _require_manager(workspace_id: int, actor) -> None:
    if actor is not None and not can("manage_settings", workspace_id, actor):
        raise PermissionError("You do not have permission to manage legal holds.")


def active_holds(workspace_id: int, *, scope: str | None = None) -> list[LegalHold]:
    """Return active holds for ``workspace_id``, optionally filtered by scope."""
    query = LegalHold.query.filter(
        LegalHold.workspace_id == workspace_id,
        LegalHold.released_at.is_(None),
    )
    if scope is not None:
        query = query.filter(LegalHold.scope.in_((scope, SCOPE_ALL)))
    return query.order_by(LegalHold.created_at, LegalHold.id).all()


def has_active_hold(workspace_id: int, scope: str) -> bool:
    """Return ``True`` when an active hold protects ``scope`` in the workspace."""
    return bool(active_holds(workspace_id, scope=scope))


def list_holds(workspace_id: int, *, include_released: bool = False) -> list[dict]:
    """Return the workspace's holds for API output."""
    query = LegalHold.query.filter_by(workspace_id=workspace_id)
    if not include_released:
        query = query.filter(LegalHold.released_at.is_(None))
    rows = query.order_by(LegalHold.created_at.desc(), LegalHold.id.desc()).all()
    return [row.to_dict() for row in rows]


def place_hold(
    workspace_id: int,
    *,
    scope,
    reason,
    actor=None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Create an active hold and audit the decision."""
    _workspace(workspace_id)
    _require_manager(workspace_id, actor)
    normalized_scope = validate_scope(scope)
    normalized_reason = validate_reason(reason)
    moment = now or datetime.now(UTC)

    row = LegalHold(
        workspace_id=workspace_id,
        scope=normalized_scope,
        reason=normalized_reason,
        actor_id=getattr(actor, "id", None),
        created_at=moment,
    )
    db.session.add(row)
    db.session.flush()
    audit.record(
        audit.LEGAL_HOLD_PLACED,
        workspace_id=workspace_id,
        user=actor,
        target_type="legal_hold",
        target_id=row.id,
        metadata={"scope": normalized_scope},
    )
    if commit:
        db.session.commit()
    return row.to_dict()


def release_hold(
    hold_id: int,
    *,
    actor=None,
    reason=None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Release an active hold, recording the actor and an optional reason."""
    row = db.session.get(LegalHold, hold_id)
    if row is None:
        raise LookupError("Legal hold not found.")
    _require_manager(row.workspace_id, actor)
    if not row.is_active:
        raise LegalHoldConflict("This legal hold is already released.")
    normalized_reason = validate_reason(reason, field="release_reason") if reason else None
    moment = now or datetime.now(UTC)

    row.released_at = moment
    row.released_by_id = getattr(actor, "id", None)
    row.release_reason = normalized_reason
    audit.record(
        audit.LEGAL_HOLD_RELEASED,
        workspace_id=row.workspace_id,
        user=actor,
        target_type="legal_hold",
        target_id=row.id,
        metadata={"scope": row.scope},
    )
    if commit:
        db.session.commit()
    return row.to_dict()


def user_workspace_ids(user) -> list[int]:
    """Return workspaces the user owns or is an active member of."""
    if user is None or getattr(user, "id", None) is None:
        return []
    owned = Workspace.query.with_entities(Workspace.id).filter_by(user_id=user.id).all()
    member = (
        WorkspaceMember.query.with_entities(WorkspaceMember.workspace_id)
        .filter_by(user_id=user.id, status=STATUS_ACTIVE)
        .all()
    )
    return sorted({row[0] for row in owned} | {row[0] for row in member})


def user_has_active_hold(user, scope: str = SCOPE_WORKSPACE) -> bool:
    """Return ``True`` when any workspace the user belongs to is under a hold."""
    return any(has_active_hold(workspace_id, scope) for workspace_id in user_workspace_ids(user))


__all__ = [
    "SCOPES",
    "SCOPE_ALL",
    "SCOPE_AUDIT",
    "SCOPE_WORKSPACE",
    "LegalHoldActive",
    "LegalHoldConflict",
    "LegalHoldError",
    "active_holds",
    "has_active_hold",
    "list_holds",
    "place_hold",
    "release_hold",
    "user_has_active_hold",
    "user_workspace_ids",
    "validate_reason",
    "validate_scope",
]
