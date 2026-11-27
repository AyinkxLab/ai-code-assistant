"""Workspace activity recording service.

A thin service that appends ``ActivityEvent`` rows alongside the actions that
produce them, so the activity feed (145), the owner audit view (151), and
notification triggers (149) all read one consistent history. Events are
append-only: nothing in the application updates or deletes them.

Only authorized actions call ``record_activity`` — the caller is responsible
for having already validated the actor can perform the action. ``metadata``
must contain only small, safe facts (role values, labels); never file contents,
tokens, or passwords.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.extensions import db
from app.models.activity_event import EVENT_ROLE_CHANGED, ActivityEvent
from app.services.chat_audit import current_request_id, log_event

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.user import User


#: Values stored in ``metadata["action"]`` so the audit view can distinguish an
#: assignment, an actual role change, and a removal without new event types.
ROLE_ACTION_ASSIGNED = "assigned"
ROLE_ACTION_CHANGED = "changed"
ROLE_ACTION_REMOVED = "removed"


def record_activity(
    workspace_id: int,
    event_type: str,
    actor: User | None = None,
    target: object | None = None,
    metadata: dict | None = None,
) -> ActivityEvent:
    """Append an activity event in the current transaction.

    ``actor`` may be ``None`` for system-originated events. ``target`` is an
    ORM object; its ``__tablename__`` (minus any trailing ``s`` heuristics are
    avoided) and primary key become ``target_type``/``target_id``.
    """
    target_type = None
    target_id = None
    if target is not None:
        target_type = getattr(target, "__tablename__", None) or type(target).__name__
        target_id = getattr(target, "id", None)
    event = ActivityEvent(
        workspace_id=workspace_id,
        actor_id=actor.id if actor is not None else None,
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        event_metadata=metadata or None,
    )
    db.session.add(event)
    return event


def record_role_change(
    *,
    workspace_id: int,
    actor: User | None,
    target_user: User | None,
    previous_role: str | None,
    new_role: str | None,
    action: str | None = None,
    membership_id: int | None = None,
    reason: str | None = None,
) -> ActivityEvent:
    """Record one role assignment, change, or removal with before/after state.

    The event is written to the existing owner audit view via ``ActivityEvent``
    and emitted as a structured ``role.changed`` log record. Both carry the
    acting user, the affected user, and the current request id. ``None`` is used
    for a state where the user had no role (assignment) or no longer has one
    (removal); callers must not omit one side to hide a transition.
    """
    if action is None:
        if previous_role is None:
            action = ROLE_ACTION_ASSIGNED
        elif new_role is None:
            action = ROLE_ACTION_REMOVED
        else:
            action = ROLE_ACTION_CHANGED

    metadata = {
        "action": action,
        "old_role": previous_role,
        "new_role": new_role,
        "actor_id": actor.id if actor is not None else None,
        "actor_username": actor.username if actor is not None else None,
        "target_user_id": target_user.id if target_user is not None else None,
        "target_username": target_user.username if target_user is not None else None,
        "request_id": current_request_id(),
    }
    if membership_id is not None:
        metadata["membership_id"] = membership_id
    if reason:
        metadata["reason"] = reason

    event = record_activity(
        workspace_id,
        EVENT_ROLE_CHANGED,
        actor=actor,
        target=target_user,
        metadata=metadata,
    )
    log_event(
        "role.changed",
        user_id=actor.id if actor is not None else None,
        workspace_id=workspace_id,
        target_user_id=target_user.id if target_user is not None else None,
        action=action,
        old_role=previous_role,
        new_role=new_role,
    )
    return event
