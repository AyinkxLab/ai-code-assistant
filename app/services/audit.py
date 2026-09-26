"""Audit logging for sensitive actions (issue #34).

Call :func:`record` from a route right before committing; it only ever appends
a new :class:`~app.models.audit_log.AuditLog` row. Existing rows are never
updated or removed, which keeps the trail append-only.
"""

from __future__ import annotations

import json

from app.extensions import db
from app.models.audit_log import AuditLog

#: Action names used across the app. Kept as constants so filters and tests
#: never rely on stringly-typed literals.
LOGIN_SUCCESS = "login_success"
LOGIN_FAILURE = "login_failure"
USER_CREATED = "user_created"
CONVERSATION_DELETED = "conversation_deleted"
PROMPT_DELETED = "prompt_deleted"


def record(
    action: str,
    *,
    user=None,
    user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    metadata: dict | None = None,
) -> AuditLog:
    """Append an audit entry to the session (does not commit).

    ``user`` may be a ``User`` instance (its id is used) or ``None`` for events
    with no authenticated actor (e.g. a failed login), in which case ``user_id``
    can still be supplied explicitly. Contextual facts belong in ``metadata``.
    """
    entry = AuditLog(
        user_id=user.id if user is not None else user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        meta=json.dumps(metadata) if metadata else None,
    )
    db.session.add(entry)
    return entry
