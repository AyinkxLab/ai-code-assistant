"""Collaboration routes: invitations, notifications, comments, activity feed,
audit log, settings, presence, and the members/audit pages.

Every endpoint routes its authorization through ``app/services/permissions.py``:

* Owner-only: invitations (create/list/cancel), settings (update), ownership
  transfer, audit read, member add/update/remove (in the workspaces blueprint).
* Member: activity feed (non-audit subset), comments, member list read, settings
  read, presence heartbeat, self-leave, members page.
* Current-user only: notifications inbox/count/read/preferences.

Security notes
--------------
* Invitation tokens are stored as SHA-256 hashes; the raw token is returned
  once at creation and delivered only via the invite email link.
* The accept/decline/landing endpoints are IP rate-limited and return uniform
  404s for unknown/expired/cancelled tokens to avoid an existence oracle.
* Audit rows are append-only; there is no write/delete route for them.
* Project collaboration access uses ``resolve_project_collab`` (owner or active
  member of the project's workspace), never raw id lookups.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import abort, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from app.collaboration import bp
from app.extensions import db
from app.models import (
    ActivityEvent,
    Notification,
    NotificationPreference,
    ProjectComment,
    ProjectMessage,
    ReviewComment,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceSettings,
)
from app.models.activity_event import (
    AUDIT_EVENT_TYPES,
    EVENT_COMMENT_ADDED,
    EVENT_INVITATION_ACCEPTED,
    EVENT_INVITATION_CANCELLED,
    EVENT_INVITATION_CREATED,
    EVENT_INVITATION_DECLINED,
    EVENT_MEMBER_LEFT,
    EVENT_OWNERSHIP_TRANSFERRED,
    EVENT_SETTINGS_CHANGED,
)
from app.models.invitation import (
    INVITE_STATUS_ACCEPTED,
    INVITE_STATUS_CANCELLED,
    INVITE_STATUS_DECLINED,
    INVITE_STATUS_PENDING,
)
from app.models.notification_preference import PREFERENCE_TYPES
from app.models.project_comment import COMMENT_MAX_LENGTH
from app.models.review_comment import REVIEW_COMMENT_MAX_LENGTH
from app.models.workspace_member import MEMBER_ROLES, ROLE_OWNER, STATUS_ACTIVE
from app.models.workspace_settings import validate_member_role
from app.services import email as email_service
from app.services import ratelimit
from app.services import review_comments as review_comments_service
from app.services.activity import record_activity
from app.services.invitations import (
    cancel_pending_for_user,
    effective_status,
    expire_overdue,
    generate_token,
    token_hash,
)
from app.services.mentions import extract_mentions
from app.services.notifications import notify
from app.services.permissions import (
    can,
    require_workspace_capability,
    require_workspace_member,
    resolve_project_collab,
    resolve_workspace,
    role_for,
)

_PER_PAGE_DEFAULT = 20
_PER_PAGE_MAX = 100


def _per_page() -> int:
    value = request.args.get("per_page", type=int) or _PER_PAGE_DEFAULT
    return max(1, min(value, _PER_PAGE_MAX))


def _page() -> int:
    return max(request.args.get("page", type=int) or 1, 1)


def _find_invitation(token: str) -> WorkspaceInvitation | None:
    """Look an invitation up by its hashed token (timing-safe by construction)."""
    if not token or len(token) > 500:
        return None
    return WorkspaceInvitation.query.filter_by(token_hash=token_hash(token)).first()


def _settings(workspace: Workspace) -> WorkspaceSettings:
    """Return (creating on demand) the workspace's settings row.

    The row is flushed so column defaults (``invitations_enabled``,
    ``default_member_role``) are materialized before callers read them.
    """
    row = WorkspaceSettings.query.filter_by(workspace_id=workspace.id).first()
    if row is None:
        row = WorkspaceSettings(workspace_id=workspace.id)
        db.session.add(row)
        db.session.flush()
    return row


def _settings_or_default(workspace_id: int) -> dict:
    row = WorkspaceSettings.query.filter_by(workspace_id=workspace_id).first()
    if row is not None:
        return row.to_dict()
    return {
        "workspace_id": workspace_id,
        "invitations_enabled": True,
        "default_member_role": "viewer",
        "created_at": None,
        "updated_at": None,
    }


def _encode_cursor(event) -> str:
    return f"{event.created_at.isoformat()}|{event.id}"


def _decode_cursor(cursor: str) -> tuple[datetime, int] | None:
    try:
        raw_ts, raw_id = cursor.rsplit("|", 1)
        return datetime.fromisoformat(raw_ts), int(raw_id)
    except (ValueError, TypeError):
        return None


def _users_by_id(user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = User.query.filter(User.id.in_(user_ids)).all()
    return {u.id: u.username for u in rows}


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@bp.route("/notifications")
@login_required
def notifications_page():
    """The user's notification inbox page."""
    return render_template("collaboration/notifications.html")


@bp.route("/<int:workspace_id>/members")
@login_required
@require_workspace_member
def members_page(workspace_id: int):
    """Team/member management page (owner manages; members read + leave)."""
    workspace = resolve_workspace(workspace_id)
    owner = db.session.get(User, workspace.user_id)
    return render_template(
        "workspaces/members.html",
        workspace=workspace,
        owner=owner,
        my_role=role_for(workspace_id, current_user),
    )


@bp.route("/<int:workspace_id>/audit")
@login_required
@require_workspace_capability("view_audit")
def audit_page(workspace_id: int):
    """Owner-only workspace audit page."""
    workspace = resolve_workspace(workspace_id)
    return render_template("workspaces/audit.html", workspace=workspace)


@bp.route("/invitations/<token>")
def invitation_landing(token: str):
    """Public invitation landing page (no login required to view)."""
    ratelimit.hit(ratelimit.client_key("invite-landing"))
    invitation = _find_invitation(token)
    state = "invalid"
    if invitation is not None:
        status = effective_status(invitation)
        if status == INVITE_STATUS_ACCEPTED:
            state = "accepted"
        elif status == INVITE_STATUS_DECLINED:
            state = "declined"
        elif status == INVITE_STATUS_CANCELLED:
            state = "cancelled"
        elif status == INVITE_STATUS_PENDING:
            state = "expired" if invitation.is_expired() else "pending"
    return render_template(
        "collaboration/invitation.html", state=state, invitation=invitation, token=token
    )


# --------------------------------------------------------------------------
# API: membership (self-leave + ownership transfer)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/membership", methods=["DELETE"])
@login_required
@require_workspace_member
def api_leave_workspace(workspace_id: int):
    """Self-service leave; the owner must transfer ownership first."""
    workspace = resolve_workspace(workspace_id)
    if role_for(workspace_id, current_user) == ROLE_OWNER:
        return jsonify({"error": "The owner cannot leave. Transfer ownership first."}), 400
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=current_user.id, status=STATUS_ACTIVE
    ).first_or_404()
    membership.mark_removed()
    cancel_pending_for_user(workspace.id, current_user.id, current_user)
    record_activity(
        workspace.id,
        EVENT_MEMBER_LEFT,
        actor=current_user,
        target=membership,
        metadata={"role": membership.role},
    )
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/workspaces/<int:workspace_id>/transfer", methods=["POST"])
@login_required
@require_workspace_capability("transfer_ownership")
def api_transfer_ownership(workspace_id: int):
    """Atomically transfer workspace ownership to an active member.

    The target becomes the owner (``Workspace.user_id`` + membership role), the
    previous owner becomes a contributor member, and every project moves to the
    new owner so the denormalized owner column stays consistent. A single commit
    keeps the transition atomic.
    """
    workspace = resolve_workspace(workspace_id)
    data = request.get_json(silent=True) or {}
    try:
        target_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid user_id is required."}), 400

    if target_id == workspace.user_id:
        return jsonify({"error": "That user already owns this workspace."}), 400

    target = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=target_id, status=STATUS_ACTIVE
    ).first()
    if target is None:
        return jsonify({"error": "The target must be an active member of this workspace."}), 400

    previous_owner = current_user

    old_membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=previous_owner.id
    ).first()
    if old_membership is not None:
        old_membership.role = "contributor"
        if old_membership.status != STATUS_ACTIVE:
            old_membership.reactivate()
    else:
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=previous_owner.id,
                role="contributor",
            )
        )

    target.role = ROLE_OWNER
    workspace.user_id = target.user_id
    for project in workspace.projects:
        project.user_id = target.user_id

    record_activity(
        workspace.id,
        EVENT_OWNERSHIP_TRANSFERRED,
        actor=previous_owner,
        target=workspace,
        metadata={
            "from_user_id": previous_owner.id,
            "from_username": previous_owner.username,
            "to_user_id": target.user_id,
            "to_username": target.user.username,
        },
    )
    notify(
        target.user,
        "role_change",
        actor=previous_owner,
        workspace=workspace,
        payload={"title": "You are now the owner of this workspace", "role": ROLE_OWNER},
        link=url_for("collaboration.members_page", workspace_id=workspace.id),
    )
    notify(
        previous_owner,
        "role_change",
        actor=target.user,
        workspace=workspace,
        payload={
            "title": f"Ownership transferred to {target.user.username}",
            "role": "contributor",
        },
        link=url_for("collaboration.members_page", workspace_id=workspace.id),
    )
    db.session.commit()
    return jsonify({"ok": True, "workspace": workspace.to_dict()})


# --------------------------------------------------------------------------
# API: presence heartbeat
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/heartbeat", methods=["POST"])
@login_required
@require_workspace_member
def api_heartbeat(workspace_id: int):
    """Record the current member's presence (cheap, rate-limited)."""
    workspace = resolve_workspace(workspace_id)
    if not ratelimit.hit(ratelimit.client_key(f"heartbeat:{current_user.id}")):
        return jsonify({"error": "Too many requests."}), 429
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=current_user.id, status=STATUS_ACTIVE
    ).first()
    if membership is not None:
        membership.last_seen_at = datetime.now(UTC)
        db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API: invitations (owner management)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/invitations", methods=["POST"])
@login_required
@require_workspace_capability("manage_invitations")
def api_create_invitation(workspace_id: int):
    """Create an invitation for an email (registered or not)."""
    workspace = resolve_workspace(workspace_id)
    settings = _settings(workspace)
    if not settings.invitations_enabled:
        return jsonify({"error": "Invitations are disabled for this workspace."}), 403

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email or "." not in email or len(email) > 255:
        return jsonify({"error": "A valid email address is required."}), 400
    if email == current_user.email.lower():
        return jsonify({"error": "You cannot invite yourself."}), 400

    role = (data.get("role") or "").strip().lower() or settings.default_member_role
    if role not in MEMBER_ROLES:
        return jsonify({"error": "Invalid role."}), 400

    user = User.query.filter_by(email=email).first()
    if user is not None:
        if user.id == workspace.user_id:
            return jsonify({"error": "The workspace owner cannot be invited."}), 400
        active = WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=user.id, status=STATUS_ACTIVE
        ).first()
        if active is not None:
            return jsonify({"error": "That user is already a member."}), 409

    expire_overdue(workspace_id)
    pending = WorkspaceInvitation.query.filter_by(
        workspace_id=workspace.id, email=email, status=INVITE_STATUS_PENDING
    ).first()
    if pending is not None and pending.is_valid():
        return jsonify({"error": "A pending invitation for that email already exists."}), 409

    from flask import current_app

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
    db.session.flush()
    record_activity(
        workspace.id,
        EVENT_INVITATION_CREATED,
        actor=current_user,
        target=invitation,
        metadata={"email": email, "role": role},
    )
    db.session.commit()

    # In-app notification for invitees who already have an account (no token
    # in the payload; the email is the only token delivery channel).
    if user is not None:
        notify(
            user,
            "invitation",
            actor=current_user,
            workspace=workspace,
            payload={
                "title": f"You've been invited to join {workspace.name}",
                "workspace": workspace.name,
                "role": role,
            },
        )
    email_service.send_invitation_email(invitation, token)

    return jsonify({**invitation.to_dict(), "token": token}), 201


@bp.route("/api/workspaces/<int:workspace_id>/invitations", methods=["GET"])
@login_required
@require_workspace_capability("manage_invitations")
def api_list_invitations(workspace_id: int):
    """List invitations, newest first, with optional status filter."""
    workspace = resolve_workspace(workspace_id)
    expire_overdue(workspace.id)
    query = WorkspaceInvitation.query.filter_by(workspace_id=workspace.id)
    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status)
    total = query.count()
    invitations = (
        query.order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .offset((_page() - 1) * _per_page())
        .limit(_per_page())
        .all()
    )
    return jsonify(
        {
            "items": [inv.to_dict() for inv in invitations],
            "total": total,
            "page": _page(),
            "per_page": _per_page(),
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/invitations/<int:invite_id>", methods=["DELETE"])
@login_required
@require_workspace_capability("manage_invitations")
def api_cancel_invitation(workspace_id: int, invite_id: int):
    """Cancel a pending invitation."""
    workspace = resolve_workspace(workspace_id)
    invitation = WorkspaceInvitation.query.filter_by(
        id=invite_id, workspace_id=workspace.id
    ).first_or_404()
    if invitation.status != INVITE_STATUS_PENDING:
        return jsonify({"error": "Only pending invitations can be cancelled."}), 409
    invitation.status = INVITE_STATUS_CANCELLED
    record_activity(
        workspace.id,
        EVENT_INVITATION_CANCELLED,
        actor=current_user,
        target=invitation,
        metadata={"email": invitation.email},
    )
    db.session.commit()
    return jsonify(invitation.to_dict())


# --------------------------------------------------------------------------
# API: invitation accept / decline (public flow)
# --------------------------------------------------------------------------


@bp.route("/api/invitations/<token>/accept", methods=["POST"])
@login_required
def invitation_accept(token: str):
    """Accept an invitation and activate the membership (atomic)."""
    if not ratelimit.hit(ratelimit.client_key("invite-accept")):
        return jsonify({"error": "Too many attempts. Please try again later."}), 429
    invitation = _find_invitation(token)
    if invitation is None:
        return jsonify({"error": "This invitation is invalid or has expired."}), 404
    if invitation.status == INVITE_STATUS_DECLINED:
        return jsonify({"error": "This invitation was declined and cannot be accepted."}), 409
    if invitation.status == INVITE_STATUS_ACCEPTED:
        membership = WorkspaceMember.query.filter_by(
            workspace_id=invitation.workspace_id, user_id=current_user.id
        ).first()
        if membership is not None and membership.status == STATUS_ACTIVE:
            return jsonify({"membership": membership.to_dict(), "idempotent": True}), 200
        return jsonify({"error": "This invitation has already been accepted."}), 409
    if invitation.status == INVITE_STATUS_CANCELLED or not invitation.is_valid():
        return jsonify({"error": "This invitation is invalid or has expired."}), 404

    if invitation.email != current_user.email.lower():
        return jsonify({"error": "This invitation is not addressed to your account."}), 403

    workspace = db.session.get(Workspace, invitation.workspace_id)
    settings = _settings(workspace)
    role = invitation.role or settings.default_member_role

    membership = WorkspaceMember.query.filter_by(
        workspace_id=invitation.workspace_id, user_id=current_user.id
    ).first()
    if membership is not None and membership.status == STATUS_ACTIVE:
        invitation.mark(INVITE_STATUS_ACCEPTED, accepted_by=current_user.id)
        record_activity(
            workspace.id,
            EVENT_INVITATION_ACCEPTED,
            actor=current_user,
            target=membership,
            metadata={"role": membership.role, "idempotent": True},
        )
        db.session.commit()
        return jsonify({"membership": membership.to_dict(), "idempotent": True}), 200

    if membership is not None:
        membership.reactivate()
        membership.role = role
    else:
        membership = WorkspaceMember(
            workspace_id=invitation.workspace_id,
            user_id=current_user.id,
            role=role,
        )
        db.session.add(membership)

    invitation.mark(INVITE_STATUS_ACCEPTED, accepted_by=current_user.id)
    record_activity(
        workspace.id,
        EVENT_INVITATION_ACCEPTED,
        actor=current_user,
        target=membership,
        metadata={"role": role},
    )
    notify(
        invitation.inviter,
        "invitation",
        actor=current_user,
        workspace=workspace,
        payload={
            "title": f"{current_user.username} accepted your invitation",
            "workspace": workspace.name,
        },
        link=url_for("collaboration.members_page", workspace_id=workspace.id),
    )
    db.session.commit()
    return jsonify({"membership": membership.to_dict()}), 201


@bp.route("/api/invitations/<token>/decline", methods=["POST"])
@login_required
def invitation_decline(token: str):
    """Decline an invitation and record the decision."""
    if not ratelimit.hit(ratelimit.client_key("invite-decline")):
        return jsonify({"error": "Too many attempts. Please try again later."}), 429
    invitation = _find_invitation(token)
    if invitation is None or invitation.status != INVITE_STATUS_PENDING or invitation.is_expired():
        return jsonify({"error": "This invitation is invalid or no longer pending."}), 404
    invitation.status = INVITE_STATUS_DECLINED
    record_activity(
        invitation.workspace_id,
        EVENT_INVITATION_DECLINED,
        actor=current_user,
        target=invitation,
        metadata={"email": invitation.email},
    )
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API: activity feed (members) + audit (owner only)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/activity", methods=["GET"])
@login_required
@require_workspace_capability("view_activity")
def api_activity_feed(workspace_id: int):
    """Member-scoped activity feed with cursor pagination and filters."""
    workspace = resolve_workspace(workspace_id)
    role = role_for(workspace_id, current_user)
    query = ActivityEvent.query.filter_by(workspace_id=workspace.id)
    if role != ROLE_OWNER:
        # Members never see the audit-sensitive subset.
        query = query.filter(ActivityEvent.event_type.notin_(AUDIT_EVENT_TYPES))

    event_type = request.args.get("event_type")
    if event_type:
        query = query.filter_by(event_type=event_type)
    actor_name = request.args.get("actor")
    if actor_name:
        actor_ids = [u.id for u in User.query.filter_by(username=actor_name).all()]
        if not actor_ids:
            return jsonify({"items": [], "next_cursor": None})
        query = query.filter(ActivityEvent.actor_id.in_(actor_ids))

    cursor = request.args.get("before")
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded is not None:
            before_ts, before_id = decoded
            query = query.filter(
                db.or_(
                    ActivityEvent.created_at < before_ts,
                    db.and_(ActivityEvent.created_at == before_ts, ActivityEvent.id < before_id),
                )
            )
        else:
            return jsonify({"error": "Invalid cursor."}), 400

    events = (
        query.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .limit(_per_page() + 1)
        .all()
    )
    has_more = len(events) > _per_page()
    events = events[: _per_page()]
    users = _users_by_id({e.actor_id for e in events if e.actor_id})
    items = [e.to_dict(actor_username=users.get(e.actor_id)) for e in events]
    next_cursor = _encode_cursor(events[-1]) if events and has_more else None
    return jsonify({"items": items, "next_cursor": next_cursor})


@bp.route("/api/workspaces/<int:workspace_id>/audit", methods=["GET"])
@login_required
@require_workspace_capability("view_audit")
def api_audit_log(workspace_id: int):
    """Owner-only audit log over the defined audit event subset."""
    workspace = resolve_workspace(workspace_id)
    query = ActivityEvent.query.filter(
        ActivityEvent.workspace_id == workspace.id,
        ActivityEvent.event_type.in_(AUDIT_EVENT_TYPES),
    )
    event_type = request.args.get("event_type")
    if event_type:
        query = query.filter_by(event_type=event_type)
    actor_name = request.args.get("actor")
    if actor_name:
        actor_ids = [u.id for u in User.query.filter_by(username=actor_name).all()]
        if actor_ids:
            query = query.filter(ActivityEvent.actor_id.in_(actor_ids))
        else:
            return jsonify({"items": [], "total": 0, "page": _page(), "per_page": _per_page()})

    total = query.count()
    events = (
        query.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .offset((_page() - 1) * _per_page())
        .limit(_per_page())
        .all()
    )
    return jsonify(
        {
            "items": [e.to_audit_dict() for e in events],
            "total": total,
            "page": _page(),
            "per_page": _per_page(),
        }
    )


# --------------------------------------------------------------------------
# API: notifications (strictly current-user scoped)
# --------------------------------------------------------------------------


@bp.route("/api/notifications", methods=["GET"])
@login_required
def api_list_notifications():
    """The current user's notification inbox."""
    query = Notification.query.filter_by(user_id=current_user.id)
    if request.args.get("unread") == "1":
        query = query.filter_by(is_read=False)
    total = query.count()
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    items = (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((_page() - 1) * _per_page())
        .limit(_per_page())
        .all()
    )
    return jsonify(
        {
            "items": [n.to_dict() for n in items],
            "total": total,
            "unread_count": unread_count,
            "page": _page(),
            "per_page": _per_page(),
        }
    )


@bp.route("/api/notifications/count", methods=["GET"])
@login_required
def api_notification_count():
    """Cheap unread count for the header badge."""
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({"unread": unread})


@bp.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def api_mark_notification_read(notification_id: int):
    """Mark one of the current user's notifications as read."""
    notification = Notification.query.filter_by(
        id=notification_id, user_id=current_user.id
    ).first_or_404()
    notification.is_read = True
    db.session.commit()
    return jsonify(notification.to_dict())


@bp.route("/api/notifications/read-all", methods=["POST"])
@login_required
def api_mark_all_read():
    """Mark all of the current user's notifications as read."""
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/notifications/preferences", methods=["GET"])
@login_required
def api_get_preferences():
    """Return the current user's notification preferences (or defaults)."""
    row = NotificationPreference.query.filter_by(user_id=current_user.id).first()
    if row is None:
        row = NotificationPreference(user_id=current_user.id)
        db.session.add(row)
        db.session.flush()
    return jsonify(row.to_dict())


@bp.route("/api/notifications/preferences", methods=["PUT"])
@login_required
def api_update_preferences():
    """Update the current user's notification preferences."""
    data = request.get_json(silent=True) or {}
    row = NotificationPreference.query.filter_by(user_id=current_user.id).first()
    if row is None:
        row = NotificationPreference(user_id=current_user.id)
        db.session.add(row)
    for key in PREFERENCE_TYPES:
        if key in data:
            row.__setattr__(key, bool(data[key]))
    db.session.commit()
    return jsonify(row.to_dict())


# --------------------------------------------------------------------------
# API: project discussion comments
# --------------------------------------------------------------------------


@bp.route("/api/projects/<int:project_id>/comments", methods=["GET"])
@login_required
def api_list_comments(project_id: int):
    """Paginated project discussion (threads and replies)."""
    project = resolve_project_collab(project_id)
    total = ProjectComment.query.filter_by(project_id=project.id).count()
    comments = (
        ProjectComment.query.filter_by(project_id=project.id)
        .order_by(ProjectComment.created_at.asc(), ProjectComment.id.asc())
        .offset((_page() - 1) * _per_page())
        .limit(_per_page())
        .all()
    )
    return jsonify(
        {
            "items": [c.to_dict() for c in comments],
            "total": total,
            "page": _page(),
            "per_page": _per_page(),
        }
    )


@bp.route("/api/projects/<int:project_id>/comments", methods=["POST"])
@login_required
def api_create_comment(project_id: int):
    """Create a project comment; mentions notify active members."""
    project = resolve_project_collab(project_id)
    workspace_id = project.workspace_id
    if not can("comment", workspace_id):
        abort(403)

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Comment content is required."}), 400
    if len(content) > COMMENT_MAX_LENGTH:
        return jsonify({"error": f"Comments are limited to {COMMENT_MAX_LENGTH} characters."}), 400

    comment = ProjectComment(project_id=project.id, author_id=current_user.id, content=content)
    parent_id = data.get("parent_id")
    if parent_id is not None:
        parent = ProjectComment.query.filter_by(id=parent_id, project_id=project.id).first()
        if parent is None:
            return jsonify({"error": "Parent comment not found in this project."}), 400
        comment.parent_id = parent.id
    db.session.add(comment)
    db.session.flush()

    workspace = db.session.get(Workspace, workspace_id)
    record_activity(
        workspace_id,
        EVENT_COMMENT_ADDED,
        actor=current_user,
        target=comment,
        metadata={"project_id": project.id, "project": project.name},
    )
    for mentioned in extract_mentions(content, workspace_id):
        if mentioned.id == current_user.id:
            continue
        notify(
            mentioned,
            "mention",
            actor=current_user,
            workspace=workspace,
            project=project,
            payload={
                "title": f"{current_user.username} mentioned you in {project.name}",
                "project": project.name,
            },
            link=url_for(
                "workspaces.project_explorer",
                workspace_id=workspace_id,
                project_id=project.id,
            ),
        )
    db.session.commit()
    return jsonify(comment.to_dict()), 201


@bp.route("/api/projects/<int:project_id>/comments/<int:comment_id>", methods=["DELETE"])
@login_required
def api_delete_comment(project_id: int, comment_id: int):
    """Delete a comment as its author or the workspace owner."""
    project = resolve_project_collab(project_id)
    comment = ProjectComment.query.filter_by(id=comment_id, project_id=project.id).first_or_404()
    is_owner = can("manage_members", project.workspace_id)
    if comment.author_id != current_user.id and not is_owner:
        return (
            jsonify({"error": "Only the author or the workspace owner can delete this comment."}),
            403,
        )
    db.session.delete(comment)
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API: inline review comments (#52)
# --------------------------------------------------------------------------


def _project_message(project, message_id: int) -> ProjectMessage:
    return ProjectMessage.query.filter_by(id=message_id, project_id=project.id).first_or_404()


def _review_thread(comment: ReviewComment) -> dict:
    payload = comment.to_dict()
    payload["replies"] = [reply.to_dict() for reply in comment.replies]
    return payload


@bp.route(
    "/api/projects/<int:project_id>/messages/<int:message_id>/review-comments",
    methods=["GET"],
)
@login_required
def api_list_review_comments(project_id: int, message_id: int):
    """List inline review threads anchored to an assistant message."""
    project = resolve_project_collab(project_id)
    message = _project_message(project, message_id)
    roots = (
        ReviewComment.query.filter_by(message_id=message.id, parent_id=None)
        .order_by(ReviewComment.created_at.asc(), ReviewComment.id.asc())
        .all()
    )
    return jsonify({"message_id": message.id, "items": [_review_thread(root) for root in roots]})


@bp.route(
    "/api/projects/<int:project_id>/messages/<int:message_id>/review-comments",
    methods=["POST"],
)
@login_required
def api_create_review_comment(project_id: int, message_id: int):
    """Create an inline comment, optionally anchored to a code block/line range."""
    project = resolve_project_collab(project_id)
    workspace_id = project.workspace_id
    if not can("comment", workspace_id):
        abort(403)
    message = _project_message(project, message_id)
    if message.role != "assistant":
        return (
            jsonify({"error": "Inline comments can only be attached to assistant messages."}),
            400,
        )

    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Comment body is required."}), 400
    if len(body) > REVIEW_COMMENT_MAX_LENGTH:
        return (
            jsonify({"error": f"Comments are limited to {REVIEW_COMMENT_MAX_LENGTH} characters."}),
            400,
        )

    anchor, error = review_comments_service.validate_anchor(data, message.content)
    if error:
        return jsonify({"error": error}), 400

    comment = ReviewComment(
        project_id=project.id,
        message_id=message.id,
        author_id=current_user.id,
        body=body,
        **anchor,
    )
    parent_id = data.get("parent_id")
    if parent_id is not None:
        parent = ReviewComment.query.filter_by(id=parent_id, message_id=message.id).first()
        if parent is None:
            return jsonify({"error": "Parent comment not found on this message."}), 400
        if parent.parent_id is not None:
            return jsonify({"error": "Replies can only be one level deep."}), 400
        comment.parent_id = parent.id

    db.session.add(comment)
    db.session.flush()

    workspace = db.session.get(Workspace, workspace_id)
    for mentioned in extract_mentions(body, workspace_id):
        if mentioned.id == current_user.id:
            continue
        notify(
            mentioned,
            "mention",
            actor=current_user,
            workspace=workspace,
            project=project,
            payload={
                "title": f"{current_user.username} mentioned you in {project.name}",
                "project": project.name,
            },
            link=url_for(
                "workspaces.project_explorer",
                workspace_id=workspace_id,
                project_id=project.id,
            ),
        )
    db.session.commit()
    return jsonify(comment.to_dict()), 201


@bp.route(
    "/api/projects/<int:project_id>/messages/<int:message_id>/review-comments/<int:comment_id>",
    methods=["PATCH"],
)
@login_required
def api_update_review_comment(project_id: int, message_id: int, comment_id: int):
    """Resolve or unresolve a review thread (author or workspace owner)."""
    project = resolve_project_collab(project_id)
    comment = ReviewComment.query.filter_by(
        id=comment_id, project_id=project.id, message_id=message_id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    if "resolved" in data:
        if comment.parent_id is not None:
            return jsonify({"error": "Only a thread root can be resolved."}), 400
        if not review_comments_service.can_resolve(comment, project):
            return (
                jsonify(
                    {"error": "Only the author or the workspace owner can resolve this thread."}
                ),
                403,
            )
        comment.resolved = bool(data["resolved"])
        if comment.resolved:
            comment.resolved_by = current_user.id
            comment.resolved_at = datetime.now(UTC)
        else:
            comment.resolved_by = None
            comment.resolved_at = None
    db.session.commit()
    return jsonify(comment.to_dict())


@bp.route(
    "/api/projects/<int:project_id>/messages/<int:message_id>/review-comments/<int:comment_id>",
    methods=["DELETE"],
)
@login_required
def api_delete_review_comment(project_id: int, message_id: int, comment_id: int):
    """Delete an inline comment as its author or the workspace owner."""
    project = resolve_project_collab(project_id)
    comment = ReviewComment.query.filter_by(
        id=comment_id, project_id=project.id, message_id=message_id
    ).first_or_404()
    if not review_comments_service.can_delete(comment, project):
        return (
            jsonify({"error": "Only the author or the workspace owner can delete this comment."}),
            403,
        )
    db.session.delete(comment)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/projects/<int:project_id>/review-summary", methods=["GET"])
@login_required
def api_review_summary(project_id: int):
    """Open/resolved inline review threads for the conversation summary panel."""
    project = resolve_project_collab(project_id)
    return jsonify(review_comments_service.conversation_review_summary(project))


# --------------------------------------------------------------------------
# API: collaboration settings
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/settings", methods=["GET"])
@login_required
@require_workspace_member
def api_get_settings(workspace_id: int):
    """Members may read the workspace collaboration settings."""
    workspace = resolve_workspace(workspace_id)
    return jsonify(_settings_or_default(workspace.id))


@bp.route("/api/workspaces/<int:workspace_id>/settings", methods=["PUT"])
@login_required
@require_workspace_capability("manage_settings")
def api_update_settings(workspace_id: int):
    """Owners may update collaboration settings (enforced downstream)."""
    workspace = resolve_workspace(workspace_id)
    settings = _settings(workspace)
    data = request.get_json(silent=True) or {}
    if "invitations_enabled" in data:
        settings.invitations_enabled = bool(data["invitations_enabled"])
    if "default_member_role" in data:
        role = validate_member_role(data["default_member_role"])
        if role is None:
            return jsonify({"error": "default_member_role must be viewer or contributor."}), 400
        settings.default_member_role = role
    record_activity(
        workspace.id,
        EVENT_SETTINGS_CHANGED,
        actor=current_user,
        target=settings,
        metadata={
            "invitations_enabled": settings.invitations_enabled,
            "default_member_role": settings.default_member_role,
        },
    )
    db.session.commit()
    return jsonify(settings.to_dict())
