"""Organization custom-role administration (#597).

Roles are scoped to the organization owner and managed from the organization
admin console. Every route requires a login and an owner/admin organization
role; all failures use the structured catalog response.
"""

from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user, login_required

from app.admin import bp
from app.admin.organizations_common import action_payload, resolve_organization
from app.api.error_catalog import error_response
from app.extensions import db
from app.services import audit, permissions
from app.services.organization_roles import (
    OrganizationRoleError,
    create_role,
    delete_role,
    list_roles,
    update_role,
)


@bp.route("/organizations/<int:workspace_id>/roles", methods=["GET", "POST"])
@login_required
def organization_roles(workspace_id: int):
    """List organization roles or create a custom role."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error

    if request.method == "GET":
        return jsonify(
            {
                "roles": [role.to_dict() for role in list_roles(workspace.user_id)],
                "capabilities": sorted(permissions.CAPABILITIES),
            }
        )

    data = action_payload()
    try:
        role = create_role(
            workspace.user_id,
            data.get("name"),
            data.get("capabilities"),
            description=data.get("description"),
            created_by_id=current_user.id,
        )
    except OrganizationRoleError as exc:
        return error_response(exc.code, str(exc))

    audit.record(
        audit.CUSTOM_ROLE_CREATED,
        workspace_id=workspace.id,
        user=current_user,
        target_type="organization_role",
        target_id=role.id,
        metadata={"name": role.name, "capabilities": role.capability_names},
    )
    db.session.commit()
    return jsonify({"role": role.to_dict()}), 201


@bp.route(
    "/organizations/<int:workspace_id>/roles/<int:role_id>",
    methods=["PATCH", "DELETE"],
)
@login_required
def organization_role_detail(workspace_id: int, role_id: int):
    """Update or delete one custom role."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error

    if request.method == "DELETE":
        try:
            delete_role(workspace.user_id, role_id)
        except OrganizationRoleError as exc:
            return error_response(exc.code, str(exc))
        audit.record(
            audit.CUSTOM_ROLE_DELETED,
            workspace_id=workspace.id,
            user=current_user,
            target_type="organization_role",
            target_id=role_id,
        )
        db.session.commit()
        return jsonify({"ok": True})

    data = action_payload()
    try:
        role = update_role(
            workspace.user_id,
            role_id,
            name=data.get("name"),
            capabilities=data.get("capabilities"),
            description=data.get("description", ...),
        )
    except OrganizationRoleError as exc:
        return error_response(exc.code, str(exc))
    audit.record(
        audit.CUSTOM_ROLE_UPDATED,
        workspace_id=workspace.id,
        user=current_user,
        target_type="organization_role",
        target_id=role.id,
        metadata={"name": role.name, "capabilities": role.capability_names},
    )
    db.session.commit()
    return jsonify({"role": role.to_dict()})
