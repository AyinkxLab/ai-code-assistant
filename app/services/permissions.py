"""Centralized workspace role/permission model.

Every Phase 7 collaboration endpoint routes its authorization through this
module so there is a single, reviewable capability map instead of ad hoc role
strings scattered across routes.

Roles
-----
* ``owner`` — workspace administration (members, invitations, settings,
  ownership transfer, audit).
* ``contributor`` — permitted development actions, no owner-level admin.
* ``viewer`` — read-only collaboration surface.

Capability model
----------------
A capability is a named action a role may perform. ``role_can`` resolves a
role to a boolean; ``can``/``has_capability`` resolve the requesting user's
role in a workspace and check it. Non-members always resolve to no
capabilities (fail closed): role resolution returns ``None``, which maps to
nothing.

``resolve_workspace`` returns the workspace for a member (or owner) or raises
404, which deliberately does not distinguish "workspace does not exist" from
"you cannot access it" to avoid an existence oracle.

The matrix is a plain dict so the members UI and the documentation can render
it without importing role logic.
"""

from __future__ import annotations

from functools import wraps

from flask import abort
from flask_login import current_user

from app.models import Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_OWNER, STATUS_ACTIVE

# Role ordering: higher rank implies every capability of lower ranks.
ROLE_ORDER = ("viewer", "contributor", "owner")
ROLE_RANK = {role: index for index, role in enumerate(ROLE_ORDER)}

# Capability -> (allowed roles, human description).
CAPABILITIES: dict[str, tuple[tuple[str, ...], str]] = {
    "view_members": ((ROLE_OWNER, "contributor", "viewer"), "View the member list"),
    "leave_workspace": (
        ("contributor", "viewer"),
        "Remove yourself from the workspace (owners transfer instead)",
    ),
    "manage_members": ((ROLE_OWNER,), "Add, remove, and update members"),
    "manage_invitations": ((ROLE_OWNER,), "Create, list, and cancel invitations"),
    "manage_settings": ((ROLE_OWNER,), "Edit workspace collaboration settings"),
    "transfer_ownership": ((ROLE_OWNER,), "Transfer workspace ownership"),
    "view_audit": ((ROLE_OWNER,), "Read the workspace audit log"),
    "comment": (
        (ROLE_OWNER, "contributor", "viewer"),
        "Create project discussion comments",
    ),
    "view_activity": (
        (ROLE_OWNER, "contributor", "viewer"),
        "Read the workspace activity feed",
    ),
    "heartbeat": (
        (ROLE_OWNER, "contributor", "viewer"),
        "Report presence in the workspace",
    ),
    "manage_plugins": ((ROLE_OWNER,), "Install, enable, disable, and grant plugin capabilities"),
}


def role_can(role: str | None, capability: str) -> bool:
    """Return ``True`` when ``role`` may perform ``capability``.

    Unknown roles and ``None`` fail closed. Only ``ROLE_ORDER`` contains
    ``owner``, so an "owner" capability can never be granted to a typo.
    """
    allowed = CAPABILITIES.get(capability)
    if allowed is None or role not in ROLE_RANK:
        return False
    return role in allowed[0]


def _owner_of(workspace_id: int) -> int | None:
    workspace = Workspace.query.filter_by(id=workspace_id).first()
    return workspace.user_id if workspace else None


def role_for(workspace_id: int, user) -> str | None:
    """Resolve ``user``'s role in the workspace, or ``None`` for non-members.

    The workspace owner is authoritative via ``Workspace.user_id`` even if a
    stale membership row exists; everyone else resolves through an *active*
    membership row.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if _owner_of(workspace_id) == user.id:
        return ROLE_OWNER
    membership = WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=user.id).first()
    if membership is None or membership.status != STATUS_ACTIVE:
        return None
    return membership.role


def can(capability: str, workspace_id: int, user=None) -> bool:
    """Return ``True`` when ``user`` may perform ``capability`` in the workspace."""
    if user is None:
        user = current_user
    return role_can(role_for(workspace_id, user), capability)


def has_capability(workspace_id: int, capability: str, user=None) -> bool:
    """Alias of :func:`can` with the workspace id first (readable in routes)."""
    return can(capability, workspace_id, user)


def resolve_workspace(workspace_id: int, user=None):
    """Return the workspace for an authorized user, or abort with 404.

    Members and the owner can resolve; everyone else gets 404 (no existence
    oracle — callers must not distinguish missing vs. inaccessible).
    """
    if user is None:
        user = current_user
    if not user.is_authenticated:
        abort(404)
    workspace = Workspace.query.filter_by(id=workspace_id).first()
    if workspace is None:
        abort(404)
    if role_for(workspace_id, user) is None:
        abort(404)
    return workspace


def require_workspace_member(view):
    """Decorator: resolve ``workspace_id`` as the route's int kwarg, 404 if not a member."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        workspace_id = kwargs.get("workspace_id")
        if workspace_id is None:
            abort(404)
        resolve_workspace(workspace_id)
        return view(*args, **kwargs)

    return wrapped


def require_workspace_capability(capability: str):
    """Decorator factory: require ``capability`` for the route's workspace."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            workspace_id = kwargs.get("workspace_id")
            if workspace_id is None:
                abort(404)
            resolve_workspace(workspace_id)
            if not can(capability, workspace_id):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def capability_roles(capability: str) -> tuple[str, ...]:
    """Return the roles allowed to perform ``capability`` (for UI rendering)."""
    entry = CAPABILITIES.get(capability)
    return entry[0] if entry else ()


def capabilities_for_role(role: str | None) -> list[str]:
    """Return the sorted list of capabilities ``role`` may perform."""
    return sorted(cap for cap in CAPABILITIES if role_can(role, cap))


def can_access_project(project, user=None) -> bool:
    """Return ``True`` when ``user`` may access ``project``'s collaboration data.

    The owner has full access; an active member of the project's workspace may
    access the project's collaboration surface (comments). Source-content tools
    (tree/file/search/chat) remain owner-scoped until member content access
    (#127) lands. Non-members always fail closed.
    """
    if user is None:
        user = current_user
    if not user.is_authenticated:
        return False
    if project.user_id == user.id:
        return True
    return role_for(project.workspace_id, user) is not None


def resolve_project_collab(project_id: int, user=None):
    """Return a project the user may collaborate on, or abort with 404.

    Deliberately returns 404 (not 403) for inaccessible projects so callers
    cannot probe project ids.
    """
    from app.models import Project

    if user is None:
        user = current_user
    project = Project.query.filter_by(id=project_id).first()
    if project is None or not can_access_project(project, user):
        abort(404)
    return project


def assert_content_access(project, user=None) -> None:
    """Fail closed (403) unless ``user`` may read ``project``'s source content.

    Source-content access is currently owner-only; member content access lands
    with #127, at which point this becomes the single gate. The AI context
    builders call this before assembling prompts so that a bypass can never
    leak project content across workspaces (fail closed on any mismatch).
    """
    if user is None:
        user = current_user
    if not getattr(user, "is_authenticated", False):
        abort(403)
    if project.user_id != user.id:
        abort(403)
