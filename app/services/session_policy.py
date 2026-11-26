"""Organization SSO session policy and per-request enforcement (#591).

Policies are keyed to a workspace (the repository's organization anchor) and
are opt-in: without a row there is no new enforcement. The guard in
:mod:`app.auth.routes` calls :func:`enforce_session_age` on every authenticated
request and :func:`requires_reauth` before sensitive actions.

Only tracked browser sessions are affected. API-token requests have no browser
session to age out and are deliberately left to the existing token scope guard.
No password or token material is stored by this module; re-authentication only
verifies the supplied password against the existing hash and records a UTC
timestamp in the signed session cookie.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from flask import g, has_request_context, request, session
from sqlalchemy import and_, or_

from app.extensions import db
from app.models import (
    OrganizationSessionPolicy,
    UserSession,
    Workspace,
    WorkspaceMember,
)
from app.models.session_policy import (
    DEFAULT_REAUTH_WINDOW_MINUTES,
    MAX_MINUTES,
    MAX_REAUTH_WINDOW_MINUTES,
)
from app.models.workspace_member import STATUS_ACTIVE
from app.services import audit, user_sessions
from app.services.organizations import ORG_ROLES, organization_role

#: Cookie key set only by the explicit sensitive-action re-authentication route.
#: A normal login does not satisfy this stricter, action-scoped requirement.
REAUTH_SESSION_KEY = user_sessions.SENSITIVE_REAUTH_KEY
MAX_AGE_REASON = "session_policy_max_age"
IDLE_REASON = "session_policy_idle"

_SENSITIVE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SENSITIVE_PREFIXES = (
    "/auth/api/data-deletion",
    "/api/data-deletion",
    "/api/approvals",
    "/api/group-roles",
    "/scim/v2/Groups",
)
_MEMBER_ROUTE = re.compile(r"^/workspaces/api/workspaces/\d+/members/\d+$")


class SessionPolicyError(ValueError):
    """Invalid policy input or failed password verification."""


class ReauthFailed(SessionPolicyError):
    """The supplied password did not verify."""


class SessionPolicyPermissionDenied(PermissionError):
    """The caller is not an organization administrator or member."""


class SessionPolicyNotFound(LookupError):
    """The workspace or policy row does not exist."""


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _minutes(value, field: str, *, maximum: int = MAX_MINUTES) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SessionPolicyError(f"{field} must be an integer number of minutes.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SessionPolicyError(f"{field} must be an integer number of minutes.") from exc
    if normalized < 1 or normalized > maximum:
        raise SessionPolicyError(f"{field} must be between 1 and {maximum} minutes.")
    return normalized


def _workspace(workspace_id) -> Workspace:
    if isinstance(workspace_id, bool) or not isinstance(workspace_id, int) or workspace_id < 1:
        raise SessionPolicyNotFound("Workspace not found.")
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise SessionPolicyNotFound("Workspace not found.")
    return workspace


def _require_admin(workspace_id: int, actor) -> None:
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise SessionPolicyPermissionDenied("Authentication is required.")
    if organization_role(workspace_id, actor) not in ORG_ROLES:
        # A global ``is_admin`` flag is deliberately ignored.
        raise SessionPolicyPermissionDenied("Organization admin access is required.")


def _is_member(workspace_id: int, actor) -> bool:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return False
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        return False
    if workspace.user_id == actor.id:
        return True
    return (
        WorkspaceMember.query.filter_by(
            workspace_id=workspace_id,
            user_id=actor.id,
            status=STATUS_ACTIVE,
        ).first()
        is not None
    )


def _row(workspace_id: int) -> OrganizationSessionPolicy | None:
    return OrganizationSessionPolicy.query.filter_by(workspace_id=workspace_id).first()


# -- Organization administration -------------------------------------------


def get_policy(workspace_id, *, actor) -> dict | None:
    """Return the organization's policy, or ``None`` when it has none."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    row = _row(workspace_id)
    return row.to_dict() if row is not None else None


def upsert_policy(
    workspace_id,
    *,
    actor,
    max_session_age_minutes=None,
    idle_timeout_minutes=None,
    require_reauth_for_sensitive_actions: bool = False,
    reauth_window_minutes=DEFAULT_REAUTH_WINDOW_MINUTES,
) -> dict:
    """Create or replace the organization's opt-in session policy."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    max_age = _minutes(max_session_age_minutes, "max_session_age_minutes")
    idle = _minutes(idle_timeout_minutes, "idle_timeout_minutes")
    window = _minutes(
        reauth_window_minutes,
        "reauth_window_minutes",
        maximum=MAX_REAUTH_WINDOW_MINUTES,
    )
    if window is None:
        raise SessionPolicyError("reauth_window_minutes is required.")

    row = _row(workspace_id)
    if row is None:
        row = OrganizationSessionPolicy(workspace_id=workspace_id)
        db.session.add(row)
    row.max_session_age_minutes = max_age
    row.idle_timeout_minutes = idle
    row.require_reauth_for_sensitive_actions = bool(require_reauth_for_sensitive_actions)
    row.reauth_window_minutes = window
    row.updated_by_id = actor.id
    db.session.flush()
    audit.record(
        audit.SESSION_POLICY_UPDATED,
        workspace_id=workspace_id,
        user=actor,
        target_type="organization_session_policy",
        target_id=row.id,
        metadata={
            "max_session_age_minutes": max_age,
            "idle_timeout_minutes": idle,
            "require_reauth": bool(require_reauth_for_sensitive_actions),
            "reauth_window_minutes": window,
        },
    )
    db.session.commit()
    return row.to_dict()


def delete_policy(workspace_id, *, actor) -> None:
    """Remove the policy, restoring the pre-policy session behaviour."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    row = _row(workspace_id)
    if row is None:
        raise SessionPolicyNotFound("Session policy not found.")
    audit.record(
        audit.SESSION_POLICY_DELETED,
        workspace_id=workspace_id,
        user=actor,
        target_type="organization_session_policy",
        target_id=row.id,
        metadata={"workspace_id": workspace_id},
    )
    db.session.delete(row)
    db.session.commit()


def reauthenticate(workspace_id, actor, password) -> dict:
    """Verify ``password`` and stamp the browser session as freshly authenticated."""
    _workspace(workspace_id)
    if not _is_member(workspace_id, actor):
        raise SessionPolicyPermissionDenied("Organization membership is required.")
    if not isinstance(password, str) or not password:
        raise SessionPolicyError("password is required.")
    if not actor.check_password(password):
        raise ReauthFailed("Incorrect password.")

    moment = datetime.now(UTC)
    session[REAUTH_SESSION_KEY] = moment.isoformat()
    audit.record(
        audit.SESSION_REAUTHENTICATED,
        workspace_id=workspace_id,
        user=actor,
        target_type="user",
        target_id=actor.id,
        metadata={"workspace_id": workspace_id},
    )
    db.session.commit()
    return {"reauthenticated_at": moment.isoformat()}


# -- Per-request enforcement -----------------------------------------------


def effective_policy(user) -> dict | None:
    """Return the strictest applicable policy for ``user``, if any.

    Owned workspaces and active memberships are joined in one query so the
    every-request guard does not add a per-request N+1 pattern (#591).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    rows = (
        OrganizationSessionPolicy.query.join(
            Workspace, OrganizationSessionPolicy.workspace_id == Workspace.id
        )
        .outerjoin(
            WorkspaceMember,
            and_(
                WorkspaceMember.workspace_id == Workspace.id,
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.status == STATUS_ACTIVE,
            ),
        )
        .filter(or_(Workspace.user_id == user.id, WorkspaceMember.id.isnot(None)))
        .distinct()
        .all()
    )
    if not rows:
        return None

    ages = [row.max_session_age_minutes for row in rows if row.max_session_age_minutes is not None]
    idles = [row.idle_timeout_minutes for row in rows if row.idle_timeout_minutes is not None]
    windows = [row.reauth_window_minutes for row in rows]
    return {
        "source_workspace_id": rows[0].workspace_id,
        "max_session_age_minutes": min(ages) if ages else None,
        "idle_timeout_minutes": min(idles) if idles else None,
        "require_reauth_for_sensitive_actions": any(
            row.require_reauth_for_sensitive_actions for row in rows
        ),
        "reauth_window_minutes": min(windows) if windows else DEFAULT_REAUTH_WINDOW_MINUTES,
    }


def _expire(reason: str, policy: dict, user, limit: int) -> None:
    user_sessions.revoke_current_session(reason=reason)
    audit.record(
        audit.SESSION_POLICY_EXPIRED,
        workspace_id=policy["source_workspace_id"],
        user=user,
        target_type="user",
        target_id=user.id,
        metadata={"reason": reason, "limit_minutes": limit},
    )
    db.session.commit()


def enforce_session_age(user) -> str | None:
    """Enforce maximum age and idle timeout; return a safe message when expired."""
    policy = effective_policy(user)
    if policy is None:
        return None
    token = user_sessions.current_token()
    if token is None:
        return None
    row = db.session.get(UserSession, token)
    if row is None or not row.is_active:
        return None

    moment = datetime.now(UTC)
    created = _as_utc(row.created_at)
    seen = _as_utc(row.last_seen_at)
    max_age = policy["max_session_age_minutes"]
    idle = policy["idle_timeout_minutes"]
    if (
        max_age is not None
        and created is not None
        and moment - created >= timedelta(minutes=max_age)
    ):
        _expire(MAX_AGE_REASON, policy, user, max_age)
        return "Maximum session age reached. Sign in again."
    if idle is not None and seen is not None and moment - seen >= timedelta(minutes=idle):
        _expire(IDLE_REASON, policy, user, idle)
        return "Idle session timeout reached. Sign in again."

    row.last_seen_at = moment
    db.session.commit()
    return None


def is_sensitive_request() -> bool:
    """Return whether the current request mutates a sensitive JSON surface."""
    if not has_request_context() or request.method not in _SENSITIVE_METHODS:
        return False
    path = request.path
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _SENSITIVE_PREFIXES):
        return True
    return _MEMBER_ROUTE.fullmatch(path) is not None


def reauth_is_fresh(policy: dict) -> bool:
    """Return whether the session has a recent successful password check."""
    value = session.get(REAUTH_SESSION_KEY)
    moment = _parse_moment(value)
    if moment is None:
        return False
    window = policy.get("reauth_window_minutes") or DEFAULT_REAUTH_WINDOW_MINUTES
    return datetime.now(UTC) - moment <= timedelta(minutes=window)


def _parse_moment(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def requires_reauth(user) -> bool:
    """Return whether the current sensitive request needs a fresh password check."""
    policy = effective_policy(user)
    if policy is None or not policy["require_reauth_for_sensitive_actions"]:
        return False
    if getattr(g, "api_token", None) is not None:
        return False
    if not is_sensitive_request():
        return False
    return not reauth_is_fresh(policy)


def clear_reauth_marker() -> None:
    """Forget the current session's re-authentication marker."""
    session.pop(REAUTH_SESSION_KEY, None)


__all__ = [
    "IDLE_REASON",
    "MAX_AGE_REASON",
    "REAUTH_SESSION_KEY",
    "ReauthFailed",
    "SessionPolicyError",
    "SessionPolicyNotFound",
    "SessionPolicyPermissionDenied",
    "clear_reauth_marker",
    "delete_policy",
    "effective_policy",
    "enforce_session_age",
    "get_policy",
    "is_sensitive_request",
    "reauth_is_fresh",
    "reauthenticate",
    "requires_reauth",
    "upsert_policy",
]
