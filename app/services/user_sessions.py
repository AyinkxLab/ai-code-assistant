"""Create, validate, and revoke tracked login sessions (#619).

Call :func:`bind_current_session` immediately after ``login_user`` so every
authenticated browser has a server-side row. ``revoke_all_for_user`` invalidates
every active row at once; the next request from any of those cookies fails the
check in :mod:`app.auth.routes`.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from flask import has_request_context, request, session

from app.extensions import db
from app.models import UserSession
from app.services import audit

#: Cookie key holding the opaque session token.
SESSION_KEY = "auth_session_id"
#: Cookie key holding the last successful password verification (#591).
LAST_AUTH_KEY = "last_auth_at"
#: Cookie key for the explicit sensitive-action re-authentication marker (#591).
SENSITIVE_REAUTH_KEY = "sensitive_reauth_at"
#: Longest accepted reason; keeps the stored/audited string bounded.
MAX_REASON_LENGTH = 255
#: Maximum stored user-agent prefix; only used for display.
MAX_USER_AGENT_LENGTH = 255


class SessionError(ValueError):
    """Invalid session operation input."""


def _normalize_reason(reason) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise SessionError("reason must be a string.")
    normalized = reason.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_REASON_LENGTH:
        raise SessionError(f"reason must be at most {MAX_REASON_LENGTH} characters.")
    return normalized


def _request_user_agent() -> str | None:
    if not has_request_context():
        return None
    value = request.headers.get("User-Agent")
    return value[:MAX_USER_AGENT_LENGTH] if value else None


def bind_current_session(user, *, commit: bool = True) -> str:
    """Create a fresh row for ``user`` and point the cookie at it."""
    if user is None or getattr(user, "id", None) is None:
        raise SessionError("A user is required to start a session.")
    token = secrets.token_urlsafe(32)
    moment = datetime.now(UTC)
    db.session.add(
        UserSession(
            id=token,
            user_id=user.id,
            created_at=moment,
            last_seen_at=moment,
            user_agent=_request_user_agent(),
        )
    )
    session[SESSION_KEY] = token
    session[LAST_AUTH_KEY] = moment.isoformat()
    if commit:
        db.session.commit()
    return token


def current_token() -> str | None:
    """Return the opaque token stored in the signed cookie, if any."""
    value = session.get(SESSION_KEY)
    return value if isinstance(value, str) and value else None


def is_active(token, user_id: int) -> bool:
    """Return ``True`` when ``token`` is a live session for ``user_id``."""
    if not isinstance(token, str) or not token:
        return False
    row = db.session.get(UserSession, token)
    return row is not None and row.user_id == user_id and row.is_active


def list_active_sessions(user) -> list[UserSession]:
    """Return the user's active sessions, newest first."""
    return (
        UserSession.query.filter_by(user_id=user.id)
        .filter(UserSession.revoked_at.is_(None))
        .order_by(UserSession.created_at.desc(), UserSession.id.desc())
        .all()
    )


def revoke_current_session(*, reason: str = "logout", commit: bool = True) -> bool:
    """Revoke the row backing the current cookie; return whether one existed."""
    token = current_token()
    if token is None:
        return False
    row = db.session.get(UserSession, token)
    if row is None or not row.is_active:
        return False
    row.revoked_at = datetime.now(UTC)
    row.revoke_reason = _normalize_reason(reason)
    session.pop(SESSION_KEY, None)
    session.pop(LAST_AUTH_KEY, None)
    session.pop(SENSITIVE_REAUTH_KEY, None)
    if commit:
        db.session.commit()
    return True


def revoke_all_for_user(
    user,
    *,
    actor=None,
    reason=None,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Revoke every active session for ``user`` and audit the decision.

    ``actor`` is who asked for the revocation (the user for self-service, an
    administrator otherwise). The audit row records the actor, the target
    account, and the reason, never the session tokens themselves.
    """
    if user is None or getattr(user, "id", None) is None:
        raise SessionError("A user is required to revoke sessions.")
    normalized_reason = _normalize_reason(reason)
    moment = now or datetime.now(UTC)
    actor_id = getattr(actor, "id", None)

    rows = (
        UserSession.query.filter_by(user_id=user.id).filter(UserSession.revoked_at.is_(None)).all()
    )
    for row in rows:
        row.revoked_at = moment
        row.revoked_by_id = actor_id
        row.revoke_reason = normalized_reason

    audit.record(
        audit.SESSIONS_REVOKED,
        user=actor,
        target_type="user",
        target_id=user.id,
        metadata={
            "reason": normalized_reason or "",
            "initiated_by": "admin" if actor_id and actor_id != user.id else "self",
            "revoked": len(rows),
        },
    )
    if commit:
        db.session.commit()
    return len(rows)


__all__ = [
    "LAST_AUTH_KEY",
    "MAX_REASON_LENGTH",
    "SENSITIVE_REAUTH_KEY",
    "SESSION_KEY",
    "SessionError",
    "bind_current_session",
    "current_token",
    "is_active",
    "list_active_sessions",
    "revoke_all_for_user",
    "revoke_current_session",
]
