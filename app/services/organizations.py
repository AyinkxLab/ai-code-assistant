"""Organization administration for the Phase 11 admin console (#626).

The repository has no separate organization table: a :class:`Workspace` *is*
the organization (see ``app/models/workspace_domain.py`` and
``app/models/audit_log.py``). This module therefore builds the smallest
organization layer on top of the existing workspace models instead of adding a
parallel permission system:

* The workspace owner (``Workspace.user_id``) is the organization owner.
* An active :class:`WorkspaceMember` whose role is ``owner`` or ``admin`` is an
  organization admin. The ``admin`` role is only stored by the admin console;
  it does not widen the workspace capability matrix.
* Everyone else is a regular member and is refused by the admin console.

Usage and budgets are scoped to the workspaces owned by the organization owner
and are always labelled as estimates, reusing the Phase 10 ledger queries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask_login import current_user

from app.extensions import db
from app.models import (
    Budget,
    Project,
    StellarSecurityFinding,
    UsageRecord,
    Workspace,
    WorkspaceMember,
)
from app.models.budget import PERIOD_MONTHLY
from app.models.workspace_member import ROLE_OWNER, STATUS_ACTIVE
from app.services import budgets as budget_service
from app.services import usage_ledger

ORG_ROLE_OWNER = "owner"
ORG_ROLE_ADMIN = "admin"
ORG_ROLES = (ORG_ROLE_OWNER, ORG_ROLE_ADMIN)
#: Membership roles that grant organization administration.
ADMIN_MEMBER_ROLES = (ROLE_OWNER, ORG_ROLE_ADMIN)

#: Usage windows allowed on the console.
DEFAULT_DAYS = 30
MAX_DAYS = 365


def normalize_days(value, default: int = DEFAULT_DAYS) -> int:
    """Coerce a console window into ``1..MAX_DAYS``, raising ``ValueError``."""
    if value is None or value == "":
        return default
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be an integer.") from exc
    if days < 1 or days > MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}.")
    return days


def organization_role(workspace_id: int, user=None) -> str | None:
    """Return the caller's organization role, or ``None`` (fail closed)."""
    if user is None:
        user = current_user
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        return None
    if workspace.user_id == user.id:
        return ORG_ROLE_OWNER
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=user.id, status=STATUS_ACTIVE
    ).first()
    if membership is not None and membership.role in ADMIN_MEMBER_ROLES:
        return ORG_ROLE_ADMIN
    return None


def is_organization_admin(workspace_id: int, user=None) -> bool:
    """Return ``True`` when the caller may use the organization admin console."""
    return organization_role(workspace_id, user) in ORG_ROLES


def organization_workspace_ids(workspace: Workspace) -> list[int]:
    """Return the workspaces that belong to the organization.

    The organization is anchored on its owner, so every workspace owned by the
    same user is part of the same organization. This keeps the migration
    additive: existing workspaces become organization workspaces with no data
    copy.
    """
    rows = Workspace.query.with_entities(Workspace.id).filter_by(user_id=workspace.user_id).all()
    return [row.id for row in rows]


def organization_members(workspace: Workspace) -> list[dict]:
    """Return the owner plus every membership row, newest activity last."""
    owner = workspace.user_id
    from app.models import User

    owner_user = db.session.get(User, owner) if owner is not None else None
    rows = [
        {
            "user_id": owner,
            "username": owner_user.username if owner_user else "deleted user",
            "role": ORG_ROLE_OWNER,
            "status": STATUS_ACTIVE,
            "joined_at": workspace.created_at,
            "is_owner": True,
        }
    ]
    memberships = (
        WorkspaceMember.query.filter_by(workspace_id=workspace.id)
        .order_by(WorkspaceMember.created_at, WorkspaceMember.id)
        .all()
    )
    for membership in memberships:
        if membership.user_id == owner:
            continue
        rows.append(
            {
                "user_id": membership.user_id,
                "username": membership.user.username if membership.user else "deleted user",
                "role": membership.role,
                "status": membership.status,
                "joined_at": membership.joined_at,
                "is_owner": False,
            }
        )
    return rows


def member_count(workspace: Workspace) -> int:
    """Count the owner and every active member."""
    active = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, status=STATUS_ACTIVE
    ).count()
    return active + 1 if workspace.user_id is not None else active


def open_security_actions(workspace: Workspace) -> int:
    """Count open high/critical security findings in the organization."""
    workspace_ids = organization_workspace_ids(workspace)
    if not workspace_ids:
        return 0
    return (
        StellarSecurityFinding.query.join(Project, Project.id == StellarSecurityFinding.project_id)
        .filter(
            Project.workspace_id.in_(workspace_ids),
            StellarSecurityFinding.severity.in_(("critical", "high")),
        )
        .count()
    )


def usage_summary(workspace: Workspace, *, days: int = DEFAULT_DAYS) -> dict:
    """Summarize Phase 10 ledger rows attributed to the organization.

    The returned summary is always flagged as an estimate: cost is derived from
    the configured price table, never from a provider invoice.
    """
    workspace_ids = organization_workspace_ids(workspace)
    since = datetime.now(UTC) - timedelta(days=days)
    query = UsageRecord.query.filter(UsageRecord.created_at >= since)
    if workspace_ids:
        query = query.filter(UsageRecord.workspace_id.in_(workspace_ids))
    else:
        query = query.filter(UsageRecord.workspace_id.is_(None))
    summary = usage_ledger.summarize_query(query)
    summary["cost_is_estimate"] = True
    summary["usage_is_estimated"] = True
    summary["days"] = days
    return summary


def budget_status(workspace: Workspace) -> list[dict]:
    """Return the active workspace budget status for the organization."""
    workspace_ids = organization_workspace_ids(workspace)
    if not workspace_ids:
        return []
    rows = (
        Budget.query.filter(
            Budget.workspace_id.in_(workspace_ids),
            Budget.user_id.is_(None),
            Budget.period == PERIOD_MONTHLY,
            Budget.is_active.is_(True),
        )
        .order_by(Budget.updated_at.desc(), Budget.id.desc())
        .all()
    )
    seen: set[int] = set()
    statuses = []
    for budget in rows:
        if budget.workspace_id in seen:
            continue
        seen.add(budget.workspace_id)
        statuses.append(budget_service.evaluate_budget(budget).to_dict())
    return statuses


def dashboard(workspace: Workspace, *, days: int = DEFAULT_DAYS) -> dict:
    """Build the single organization overview payload (#626)."""
    workspace_ids = organization_workspace_ids(workspace)
    return {
        "member_count": member_count(workspace),
        "workspace_count": len(workspace_ids),
        "usage": usage_summary(workspace, days=days),
        "open_security_actions": open_security_actions(workspace),
        "days": days,
    }


__all__ = [
    "ADMIN_MEMBER_ROLES",
    "DEFAULT_DAYS",
    "MAX_DAYS",
    "ORG_ROLES",
    "ORG_ROLE_ADMIN",
    "ORG_ROLE_OWNER",
    "budget_status",
    "dashboard",
    "is_organization_admin",
    "member_count",
    "normalize_days",
    "open_security_actions",
    "organization_members",
    "organization_role",
    "organization_workspace_ids",
    "usage_summary",
]
