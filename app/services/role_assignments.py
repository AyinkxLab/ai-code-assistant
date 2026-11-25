"""Custom role assignment and deterministic precedence (#599).

Precedence (most specific first):

1. workspace assignment
2. team assignment
3. organization assignment
4. built-in ``WorkspaceMember.role``

A custom role starts from ``viewer`` or ``contributor`` capabilities, adds its
explicit grants, and removes its explicit denies. Capabilities are never unioned
across scopes: the winning assignment replaces the built-in role entirely. The
workspace owner is authoritative and cannot be demoted by a custom role.
"""

from __future__ import annotations

from app.api.error_catalog import CONFLICT, FORBIDDEN, INVALID_INPUT, NOT_FOUND
from app.extensions import db
from app.models import (
    CustomRole,
    RoleAssignment,
    Team,
    TeamMember,
    User,
    Workspace,
    WorkspaceMember,
)
from app.models.custom_role import (
    BASE_ROLES,
    SCOPE_ORGANIZATION,
    SCOPE_TEAM,
    SCOPE_WORKSPACE,
    SCOPES,
    STATUS_ACTIVE,
    STATUS_REMOVED,
)
from app.models.workspace_member import STATUS_ACTIVE as MEMBER_STATUS_ACTIVE
from app.services import organizations


class RoleAssignmentError(ValueError):
    """Raised for invalid custom role input or a conflicting lifecycle action."""

    def __init__(self, message: str, code: str = INVALID_INPUT) -> None:
        super().__init__(message)
        self.code = code


def _require_admin(workspace: Workspace, actor) -> None:
    if organizations.organization_role(workspace.id, actor) not in organizations.ORG_ROLES:
        raise RoleAssignmentError("Organization admin access required.", code=FORBIDDEN)


def _scope_id(workspace: Workspace, scope: str, team_id=None) -> int:
    scope = (scope or "").strip().lower()
    if scope == SCOPE_ORGANIZATION:
        return workspace.user_id
    if scope == SCOPE_WORKSPACE:
        return workspace.id
    if scope == SCOPE_TEAM:
        try:
            team_id = int(team_id)
        except (TypeError, ValueError):
            raise RoleAssignmentError("team_id must be an integer.") from None
        team = Team.query.filter_by(id=team_id, workspace_id=workspace.id).first()
        if team is None:
            raise RoleAssignmentError("Team not found.", code=NOT_FOUND)
        return team.id
    raise RoleAssignmentError(f"Invalid role scope: {scope or '<empty>'}.")


def _normalize_name(value) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 100:
        raise RoleAssignmentError("A role name of 1..100 characters is required.")
    return name


def _capability_list(values, field: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    from app.services.permissions import CAPABILITIES

    clean = []
    for value in values:
        capability = str(value or "").strip().lower()
        if capability not in CAPABILITIES:
            raise RoleAssignmentError(f"Unknown capability in {field}: {capability or '<empty>'}.")
        if capability not in clean:
            clean.append(capability)
    return sorted(clean)


def create_team(workspace: Workspace, actor, *, name) -> Team:
    """Create a team in ``workspace`` (organization admin only)."""
    _require_admin(workspace, actor)
    name = _normalize_name(name)
    if Team.query.filter_by(workspace_id=workspace.id, name=name).first() is not None:
        raise RoleAssignmentError("A team with that name already exists.", code=CONFLICT)
    team = Team(workspace_id=workspace.id, name=name, created_by_id=actor.id)
    db.session.add(team)
    db.session.commit()
    return team


def add_team_member(workspace: Workspace, actor, *, team_id, user_id) -> TeamMember:
    """Add or reactivate a user in a team (organization admin only)."""
    _require_admin(workspace, actor)
    scope_id = _scope_id(workspace, SCOPE_TEAM, team_id)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise RoleAssignmentError("user_id must be an integer.") from None
    if db.session.get(User, user_id) is None:
        raise RoleAssignmentError("User not found.", code=NOT_FOUND)
    row = TeamMember.query.filter_by(team_id=scope_id, user_id=user_id).first()
    if row is None:
        row = TeamMember(team_id=scope_id, user_id=user_id)
        db.session.add(row)
    else:
        row.reactivate()
    db.session.commit()
    return row


def remove_team_member(workspace: Workspace, actor, *, team_id, user_id) -> TeamMember:
    """Soft-remove a team member (organization admin only)."""
    _require_admin(workspace, actor)
    scope_id = _scope_id(workspace, SCOPE_TEAM, team_id)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise RoleAssignmentError("user_id must be an integer.") from None
    row = TeamMember.query.filter_by(team_id=scope_id, user_id=user_id).first()
    if row is None:
        raise RoleAssignmentError("Team member not found.", code=NOT_FOUND)
    if row.status == STATUS_REMOVED:
        raise RoleAssignmentError("That team member is already removed.", code=CONFLICT)
    row.mark_removed()
    db.session.commit()
    return row


def create_role(
    workspace: Workspace,
    actor,
    *,
    scope,
    name,
    base_role="viewer",
    granted=None,
    denied=None,
    team_id=None,
) -> CustomRole:
    """Create a custom role at one scope (organization admin only)."""
    _require_admin(workspace, actor)
    scope = (scope or "").strip().lower()
    if scope not in SCOPES:
        raise RoleAssignmentError(f"Invalid role scope: {scope or '<empty>'}.")
    base_role = (base_role or "").strip().lower()
    if base_role not in BASE_ROLES:
        raise RoleAssignmentError("A custom role cannot inherit owner capabilities.")
    name = _normalize_name(name)
    scope_id = _scope_id(workspace, scope, team_id)
    if CustomRole.query.filter_by(scope=scope, scope_id=scope_id, name=name).first() is not None:
        raise RoleAssignmentError(
            "A role with that name already exists at this scope.", code=CONFLICT
        )
    role = CustomRole(
        scope=scope,
        scope_id=scope_id,
        name=name,
        base_role=base_role,
        granted_capabilities=_capability_list(granted, "granted_capabilities"),
        denied_capabilities=_capability_list(denied, "denied_capabilities"),
        created_by_id=actor.id,
    )
    db.session.add(role)
    db.session.commit()
    return role


def _eligible(workspace: Workspace, user_id: int, scope: str, scope_id: int) -> bool:
    if user_id == workspace.user_id:
        return False
    if scope == SCOPE_TEAM:
        return (
            TeamMember.query.filter_by(
                team_id=scope_id, user_id=user_id, status=MEMBER_STATUS_ACTIVE
            ).first()
            is not None
        )
    return (
        WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=user_id, status=MEMBER_STATUS_ACTIVE
        ).first()
        is not None
    )


def assign_role(
    workspace: Workspace, actor, *, role_id, user_id, scope, team_id=None
) -> RoleAssignment:
    """Assign a custom role, replacing any active assignment at the same scope."""
    _require_admin(workspace, actor)
    scope = (scope or "").strip().lower()
    scope_id = _scope_id(workspace, scope, team_id)
    try:
        role_id, user_id = int(role_id), int(user_id)
    except (TypeError, ValueError):
        raise RoleAssignmentError("role_id and user_id must be integers.") from None
    role = db.session.get(CustomRole, role_id)
    if role is None or not role.active:
        raise RoleAssignmentError("Role not found.", code=NOT_FOUND)
    if role.scope != scope or role.scope_id != scope_id:
        raise RoleAssignmentError("That role does not belong to the requested scope.")
    if not _eligible(workspace, user_id, scope, scope_id):
        raise RoleAssignmentError("The user is not an active member at that scope.")
    row = RoleAssignment.query.filter_by(scope=scope, scope_id=scope_id, user_id=user_id).first()
    if row is None:
        row = RoleAssignment(scope=scope, scope_id=scope_id, user_id=user_id, role_id=role_id)
        db.session.add(row)
    else:
        row.role_id = role_id
        row.status = STATUS_ACTIVE
    row.assigned_by_id = actor.id
    db.session.commit()
    return row


def revoke_role(workspace: Workspace, actor, *, user_id, scope, team_id=None) -> RoleAssignment:
    """Deactivate a custom role assignment, restoring the built-in role."""
    _require_admin(workspace, actor)
    scope = (scope or "").strip().lower()
    scope_id = _scope_id(workspace, scope, team_id)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise RoleAssignmentError("user_id must be an integer.") from None
    row = RoleAssignment.query.filter_by(scope=scope, scope_id=scope_id, user_id=user_id).first()
    if row is None:
        raise RoleAssignmentError("Assignment not found.", code=NOT_FOUND)
    if row.status == STATUS_REMOVED:
        raise RoleAssignmentError("That assignment is already revoked.", code=CONFLICT)
    row.status = STATUS_REMOVED
    db.session.commit()
    return row


def _team_ids(workspace: Workspace, user) -> list[int]:
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    rows = (
        db.session.query(Team.id)
        .join(TeamMember, Team.id == TeamMember.team_id)
        .filter(
            Team.workspace_id == workspace.id,
            TeamMember.user_id == user.id,
            TeamMember.status == MEMBER_STATUS_ACTIVE,
        )
        .all()
    )
    return [row.id for row in rows]


def active_assignments_for(workspace: Workspace, user) -> list[RoleAssignment]:
    """Return the user's active assignments that apply to ``workspace``."""
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    if workspace.user_id == user.id:
        return []  # The owner is authoritative and cannot be demoted.
    team_ids = set(_team_ids(workspace, user))
    rows = RoleAssignment.query.filter_by(user_id=user.id, status=STATUS_ACTIVE).all()
    applicable = []
    for row in rows:
        if not row.is_active:
            continue
        if (row.scope == SCOPE_WORKSPACE and row.scope_id == workspace.id) or (
            row.scope == SCOPE_ORGANIZATION and row.scope_id == workspace.user_id
        ):
            eligible = WorkspaceMember.query.filter_by(
                workspace_id=workspace.id, user_id=user.id, status=MEMBER_STATUS_ACTIVE
            ).first()
        elif row.scope == SCOPE_TEAM and row.scope_id in team_ids:
            eligible = True
        else:
            eligible = False
        if eligible is not None and eligible is not False:
            applicable.append(row)
    return applicable


def effective_assignment(workspace: Workspace, user) -> RoleAssignment | None:
    """Resolve one assignment by deterministic scope precedence."""
    applicable = active_assignments_for(workspace, user)
    for scope in (SCOPE_WORKSPACE, SCOPE_TEAM, SCOPE_ORGANIZATION):
        candidates = [row for row in applicable if row.scope == scope]
        if candidates:
            # Highest id is the most recently created active row at that scope.
            return max(candidates, key=lambda row: row.id)
    return None


def effective_capabilities(workspace: Workspace, user) -> set[str] | None:
    """Return custom capabilities, or ``None`` when the built-in role applies."""
    assignment = effective_assignment(workspace, user)
    if assignment is None:
        return None
    return assignment.role.capability_set()


def list_roles(workspace: Workspace) -> list[dict]:
    """Return visible roles for the organization admin console."""
    scope_ids = {
        SCOPE_ORGANIZATION: {workspace.user_id},
        SCOPE_WORKSPACE: {workspace.id},
        SCOPE_TEAM: set(_team_ids_by_workspace(workspace.id)),
    }
    roles = []
    for role in CustomRole.query.filter_by(active=True).order_by(CustomRole.id).all():
        if role.scope_id in scope_ids.get(role.scope, set()):
            roles.append(role.to_dict())
    return roles


def _team_ids_by_workspace(workspace_id: int) -> list[int]:
    return [row.id for row in Team.query.filter_by(workspace_id=workspace_id).all()]


def list_assignments(workspace: Workspace) -> list[dict]:
    """Return active assignments applying to ``workspace``."""
    visible_scopes = {
        (SCOPE_ORGANIZATION, workspace.user_id),
        (SCOPE_WORKSPACE, workspace.id),
    }
    visible_scopes.update((SCOPE_TEAM, team_id) for team_id in _team_ids_by_workspace(workspace.id))
    rows = RoleAssignment.query.filter_by(status=STATUS_ACTIVE).order_by(RoleAssignment.id).all()
    return [
        row.to_dict()
        for row in rows
        if (row.scope, row.scope_id) in visible_scopes and row.is_active
    ]


def list_teams(workspace: Workspace) -> list[dict]:
    """Return teams in ``workspace``."""
    return [
        team.to_dict()
        for team in Team.query.filter_by(workspace_id=workspace.id).order_by(Team.id).all()
    ]


__all__ = [
    "RoleAssignmentError",
    "active_assignments_for",
    "add_team_member",
    "assign_role",
    "create_role",
    "create_team",
    "effective_assignment",
    "effective_capabilities",
    "list_assignments",
    "list_roles",
    "list_teams",
    "remove_team_member",
    "revoke_role",
]
