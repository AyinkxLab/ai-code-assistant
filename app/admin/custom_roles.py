"""Custom role and team assignment API for the organization console (#599)."""

from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user, login_required

from app.admin import bp
from app.admin.organizations_common import action_payload, resolve_organization, success
from app.api.error_catalog import INVALID_INPUT, error_response
from app.services import role_assignments
from app.services.role_assignments import RoleAssignmentError


@bp.route("/organizations/<int:workspace_id>/roles", methods=["GET", "POST"])
@login_required
def organization_roles(workspace_id: int):
    """List or manage custom roles, teams, and assignments for an organization."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error

    if request.method == "GET":
        return jsonify(
            {
                "roles": role_assignments.list_roles(workspace),
                "assignments": role_assignments.list_assignments(workspace),
                "teams": role_assignments.list_teams(workspace),
            }
        )

    data = action_payload()
    action = str(data.get("action") or "").strip().lower()
    handlers = {
        "create_team": _create_team,
        "add_team_member": _add_team_member,
        "remove_team_member": _remove_team_member,
        "create_role": _create_role,
        "assign": _assign,
        "revoke": _revoke,
    }
    handler = handlers.get(action)
    if handler is None:
        return error_response(INVALID_INPUT, "Unknown role action.")
    try:
        result = handler(workspace, data)
    except RoleAssignmentError as exc:
        return error_response(exc.code, str(exc))
    return success(
        "Custom role settings updated.",
        payload=result.to_dict() if hasattr(result, "to_dict") else None,
    )


def _create_team(workspace, data):
    return role_assignments.create_team(workspace, current_user, name=data.get("name"))


def _add_team_member(workspace, data):
    return role_assignments.add_team_member(
        workspace, current_user, team_id=data.get("team_id"), user_id=data.get("user_id")
    )


def _remove_team_member(workspace, data):
    return role_assignments.remove_team_member(
        workspace, current_user, team_id=data.get("team_id"), user_id=data.get("user_id")
    )


def _create_role(workspace, data):
    return role_assignments.create_role(
        workspace,
        current_user,
        scope=data.get("scope"),
        name=data.get("name"),
        base_role=data.get("base_role", "viewer"),
        granted=data.get("granted_capabilities"),
        denied=data.get("denied_capabilities"),
        team_id=data.get("team_id"),
    )


def _assign(workspace, data):
    return role_assignments.assign_role(
        workspace,
        current_user,
        role_id=data.get("role_id"),
        user_id=data.get("user_id"),
        scope=data.get("scope"),
        team_id=data.get("team_id"),
    )


def _revoke(workspace, data):
    return role_assignments.revoke_role(
        workspace,
        current_user,
        user_id=data.get("user_id"),
        scope=data.get("scope"),
        team_id=data.get("team_id"),
    )
