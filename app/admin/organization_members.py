"""Organization member management UI (#627)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import current_app, render_template, request
from flask_login import current_user, login_required

from app.admin import bp
from app.admin.organizations_common import (
    ORG_ASSIGNABLE_ROLES,
    action_payload,
    confirmed,
    resolve_organization,
    success,
)
from app.api.error_catalog import CONFLICT, INVALID_INPUT, NOT_FOUND, error_response
from app.extensions import db
from app.models import User, Workspace, WorkspaceInvitation, WorkspaceMember
from app.models.invitation import INVITE_STATUS_PENDING
from app.models.workspace_member import STATUS_ACTIVE
from app.services import email as email_service
from app.services import organizations
from app.services.invitations import (
    cancel_pending_for_user,
    expire_overdue,
    generate_token,
    token_hash,
)


@bp.route("/organizations/<int:workspace_id>/members", methods=["GET", "POST"])
@login_required
def organization_members(workspace_id: int):
    """Member management UI: invite, suspend, remove, and change roles."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error
    if request.method == "POST":
        return _handle_member_action(workspace)

    members = organizations.organization_members(workspace)
    invitations = (
        WorkspaceInvitation.query.filter_by(workspace_id=workspace.id, status=INVITE_STATUS_PENDING)
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .all()
    )
    return render_template(
        "admin/organization_members.html",
        workspace=workspace,
        members=members,
        invitations=invitations,
        assignable_roles=ORG_ASSIGNABLE_ROLES,
    )


def _handle_member_action(workspace: Workspace):
    data = action_payload()
    action = str(data.get("action") or "").strip().lower()
    if action == "invite":
        return _invite_member(workspace, data)
    if action not in {"suspend", "remove", "role", "reactivate"}:
        return error_response(INVALID_INPUT, "Unknown member action.")

    user_id = data.get("user_id")
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return error_response(INVALID_INPUT, "user_id must be an integer.")

    if user_id == workspace.user_id:
        return error_response(INVALID_INPUT, "The organization owner cannot be changed.")
    membership = WorkspaceMember.query.filter_by(workspace_id=workspace.id, user_id=user_id).first()
    if membership is None:
        return error_response(NOT_FOUND, "Member not found.")
    if membership.role == "owner":
        return error_response(INVALID_INPUT, "The organization owner cannot be changed.")

    if action == "reactivate":
        if membership.status == STATUS_ACTIVE:
            return error_response(CONFLICT, "That member is already active.")
        membership.reactivate()
        db.session.commit()
        return success(f"Restored {membership.user.username if membership.user else user_id}.")

    if not confirmed():
        return error_response(INVALID_INPUT, "This action requires confirmation.")

    if action in {"suspend", "remove"}:
        if membership.status != STATUS_ACTIVE:
            return error_response(CONFLICT, "That member is not active.")
        membership.mark_removed()
        if action == "remove":
            cancel_pending_for_user(workspace.id, membership.user_id, current_user)
        db.session.commit()
        verb = "Suspended" if action == "suspend" else "Removed"
        return success(f"{verb} {membership.user.username if membership.user else user_id}.")

    role = str(data.get("role") or "").strip().lower()
    if role not in ORG_ASSIGNABLE_ROLES:
        return error_response(INVALID_INPUT, "Invalid role.")
    if membership.status != STATUS_ACTIVE:
        return error_response(CONFLICT, "That member is not active.")
    membership.role = role
    db.session.commit()
    return success(f"Changed role to {role}.")


def _invite_member(workspace: Workspace, data: dict):
    email = str(data.get("email") or "").strip().lower()
    if not email or "@" not in email or "." not in email or len(email) > 255:
        return error_response(INVALID_INPUT, "A valid email address is required.")
    if email == (current_user.email or "").lower():
        return error_response(INVALID_INPUT, "You cannot invite yourself.")
    role = str(data.get("role") or "viewer").strip().lower()
    if role not in ORG_ASSIGNABLE_ROLES:
        return error_response(INVALID_INPUT, "Invalid role.")

    invitee = User.query.filter_by(email=email).first()
    if invitee is not None and invitee.id == workspace.user_id:
        return error_response(INVALID_INPUT, "The organization owner cannot be invited.")
    if invitee is not None:
        active = WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=invitee.id, status=STATUS_ACTIVE
        ).first()
        if active is not None:
            return error_response(CONFLICT, "That user is already a member.")

    expire_overdue(workspace.id)
    pending = WorkspaceInvitation.query.filter_by(
        workspace_id=workspace.id, email=email, status=INVITE_STATUS_PENDING
    ).first()
    if pending is not None and pending.is_valid():
        return error_response(CONFLICT, "A pending invitation for that email already exists.")

    ttl_hours = max(1, int(current_app.config.get("INVITE_TTL_HOURS", 168)))
    token = generate_token()
    invitation = WorkspaceInvitation(
        workspace_id=workspace.id,
        invited_by=current_user.id,
        email=email,
        role=role,
        token_hash=token_hash(token),
        status=INVITE_STATUS_PENDING,
        expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
    )
    db.session.add(invitation)
    db.session.commit()
    email_service.send_invitation_email(invitation, token)
    return success(
        f"Invited {email} as {role}.",
        payload={**invitation.to_dict(), "token": token},
        status=201,
    )
