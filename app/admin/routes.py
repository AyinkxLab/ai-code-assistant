"""Admin routes: the audit log viewer (issue #34).

The audit log is append-only: this blueprint only ever *reads* entries. There
is deliberately no edit or delete route, for admins or anyone else.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import wraps

from flask import Response, jsonify, render_template, request, stream_with_context
from flask_login import current_user, login_required

from app.admin import bp
from app.api.error_catalog import FORBIDDEN, INVALID_INPUT, NOT_FOUND, UNAUTHORIZED, error_response
from app.extensions import db
from app.models import AuditLog, UsageRecord, User, Workspace
from app.services import access_review as access_review_service
from app.services import audit as audit_service
from app.services import audit_export, usage_dashboards, usage_ledger, user_sessions
from app.services.classification import export_record
from app.services.currency import get_currency_config

#: Hard cap on page size so an admin cannot force an unbounded scan.
MAX_PER_PAGE = 200

#: Bounds for the detailed usage report table (#480).
REPORT_DEFAULT_PER_PAGE = 50
REPORT_MAX_PER_PAGE = 100
REPORT_SCOPES = ("all", "user", "workspace")

#: Bounds for the admin action audit view (#632).
ACTIONS_DEFAULT_PER_PAGE = 50
ACTIONS_MAX_PER_PAGE = 100


def admin_required(view):
    """Reject non-admin callers with a 403 JSON error."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        if not current_user.is_admin:
            return error_response(FORBIDDEN, "Admin access required.")
        return view(*args, **kwargs)

    return wrapped


@bp.route("/audit")
@login_required
@admin_required
def audit():
    """List audit entries (admin only), filterable by user and action.

    Query parameters:
        ``user_id``  filter to a single actor id.
        ``username`` filter to a single actor username.
        ``action``   filter to one action name.
        ``page`` / ``per_page`` pagination (``per_page`` capped at
        :data:`MAX_PER_PAGE`).
    """
    query = AuditLog.query

    user_id = request.args.get("user_id", type=int)
    username = (request.args.get("username") or "").strip()
    action = (request.args.get("action") or "").strip()

    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if username:
        actor = User.query.filter_by(username=username).first()
        # Unknown username yields an empty page rather than revealing whether
        # the account exists.
        query = query.filter(AuditLog.user_id == (actor.id if actor else -1))
    if action:
        query = query.filter(AuditLog.action == action)

    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = min(max(1, request.args.get("per_page", 50, type=int) or 50), MAX_PER_PAGE)

    pagination = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify(
        {
            "items": [entry.to_dict() for entry in pagination.items],
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "total_pages": pagination.pages,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }
    )


@bp.route("/audit/export")
@login_required
@admin_required
def audit_export_download():
    """Stream a filtered audit export as JSON or CSV (admin only).

    Query parameters:
        ``format``  ``json`` (default) or ``csv``.
        ``actor``   exact username.
        ``action``  exact action name.
        ``resource_type`` / ``resource_id`` filter the target.
        ``start`` / ``end`` inclusive ``YYYY-MM-DD`` dates.
        ``limit``   maximum rows, capped at
        :data:`~app.services.audit_export.MAX_LIMIT`.

    The projection is fixed and passes through
    :mod:`app.services.classification` at ``internal``: actor usernames,
    metadata blobs, secrets, and prompt content are never exported.
    """
    export_format = (request.args.get("format") or audit_export.FORMAT_JSON).strip().lower()
    if export_format not in audit_export.FORMATS:
        return error_response(INVALID_INPUT, "format must be json or csv.")

    try:
        query, limit = audit_export.build_query(
            actor=request.args.get("actor"),
            action=request.args.get("action"),
            start=request.args.get("start"),
            end=request.args.get("end"),
            resource_type=request.args.get("resource_type"),
            resource_id=request.args.get("resource_id"),
            limit=request.args.get("limit"),
        )
    except ValueError as exc:
        return error_response(INVALID_INPUT, str(exc))

    audit_service.record(
        audit_service.AUDIT_EXPORTED,
        user=current_user,
        target_type="audit_export",
        metadata={
            "format": export_format,
            "actor": (request.args.get("actor") or "").strip() or None,
            "action": (request.args.get("action") or "").strip() or None,
            "start": request.args.get("start"),
            "end": request.args.get("end"),
            "limit": limit,
        },
    )
    db.session.commit()

    if export_format == audit_export.FORMAT_CSV:
        body = audit_export.stream_csv(query, limit)
        mimetype = "text/csv"
        filename = "audit-export.csv"
    else:
        body = audit_export.stream_json(query, limit)
        mimetype = "application/json"
        filename = "audit-export.json"

    response = Response(stream_with_context(body), mimetype=mimetype)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Export-Limit"] = str(limit)
    return response


@bp.route("/actions")
@login_required
@admin_required
def actions():
    """Read-only admin action audit view with actor, action, and date filters.

    Only whitelisted columns are rendered: the append-only metadata blob (which
    may contain request context) is never read here. ``format=json`` returns the
    same whitelisted rows for tooling.
    """
    actor = (request.args.get("actor") or "").strip()
    action = (request.args.get("action") or "").strip()
    start_raw = (request.args.get("start") or "").strip()
    end_raw = (request.args.get("end") or "").strip()

    try:
        start = date.fromisoformat(start_raw) if start_raw else None
        end = date.fromisoformat(end_raw) if end_raw else None
    except ValueError:
        return error_response(INVALID_INPUT, "Dates must use YYYY-MM-DD.")
    if start is not None and end is not None and start > end:
        return error_response(INVALID_INPUT, "start must not be after end.")

    query = AuditLog.query
    if actor:
        user = User.query.filter_by(username=actor).first()
        # An unknown actor yields an empty page rather than revealing whether
        # the account exists.
        query = query.filter(AuditLog.user_id == (user.id if user else -1))
    if action:
        query = query.filter(AuditLog.action == action)
    if start is not None:
        query = query.filter(AuditLog.created_at >= datetime.combine(start, time.min, tzinfo=UTC))
    if end is not None:
        query = query.filter(
            AuditLog.created_at < datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        )

    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = min(
        max(
            1,
            request.args.get("per_page", ACTIONS_DEFAULT_PER_PAGE, type=int)
            or ACTIONS_DEFAULT_PER_PAGE,
        ),
        ACTIONS_MAX_PER_PAGE,
    )
    pagination = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    rows = [
        {
            "id": entry.id,
            "created_at": entry.created_at,
            "actor": entry.user.username if entry.user else "deleted user",
            "action": entry.action,
            "target_type": entry.target_type,
            "target_id": entry.target_id,
        }
        for entry in pagination.items
    ]

    if request.args.get("format") == "json":
        return jsonify(
            {
                "items": [
                    export_record(
                        {
                            **row,
                            "created_at": (
                                row["created_at"].isoformat() if row["created_at"] else None
                            ),
                        },
                        max_level="internal",
                    )
                    for row in rows
                ],
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "total_pages": pagination.pages,
            }
        )

    return render_template(
        "admin/actions.html",
        rows=rows,
        actor=actor,
        action=action,
        start=start_raw,
        end=end_raw,
        per_page=per_page,
        pagination=pagination,
    )


@bp.route("/usage")
@login_required
@admin_required
def usage():
    """Return the org-wide usage and cost dashboard (admin only).

    Aggregates every stored ``UsageRecord`` row in the requested window. This is
    read-only and never joins conversation content.
    """
    try:
        days = usage_dashboards.normalize_days(request.args.get("days"))
    except ValueError as exc:
        return error_response(INVALID_INPUT, str(exc))
    return jsonify(usage_dashboards.admin_dashboard(days=days))


@bp.route("/usage/report")
@login_required
@admin_required
def usage_report():
    """Render the admin usage report page with filters and a bounded table.

    Query parameters:
        ``days``         date window, 1-365 days (default 30).
        ``scope``        ``all``, ``user`` or ``workspace``.
        ``user_id``      required when ``scope=user``.
        ``workspace_id`` required when ``scope=workspace``.
        ``source`` / ``provider`` / ``model`` optional exact filters.
        ``page`` / ``per_page`` bounded pagination (``per_page`` capped at
        :data:`REPORT_MAX_PER_PAGE`).

    All figures come from stored :class:`~app.models.UsageRecord` rows. The
    page never reads prompts or completions.
    """
    try:
        days = usage_dashboards.normalize_days(request.args.get("days"))
    except ValueError as exc:
        return error_response(INVALID_INPUT, str(exc))

    scope = (request.args.get("scope") or "all").strip().lower()
    if scope not in REPORT_SCOPES:
        return error_response(INVALID_INPUT, "scope must be all, user or workspace.")

    user_id = request.args.get("user_id", type=int)
    workspace_id = request.args.get("workspace_id", type=int)
    filters = {
        "source": usage_dashboards.normalize_filter(request.args.get("source")),
        "provider": usage_dashboards.normalize_filter(request.args.get("provider")),
        "model": usage_dashboards.normalize_filter(request.args.get("model")),
    }

    current = datetime.now(UTC)
    query = UsageRecord.query.filter(
        UsageRecord.created_at >= current - timedelta(days=days),
        UsageRecord.created_at <= current,
    )
    if scope == "user":
        # An unknown or missing id yields an empty report rather than all rows.
        query = query.filter(UsageRecord.user_id == (user_id if user_id is not None else -1))
    elif scope == "workspace":
        query = query.filter(
            UsageRecord.workspace_id == (workspace_id if workspace_id is not None else -1)
        )
    if filters["source"]:
        query = query.filter(UsageRecord.source == filters["source"])
    if filters["provider"]:
        query = query.filter(UsageRecord.provider == filters["provider"])
    if filters["model"]:
        query = query.filter(UsageRecord.model == filters["model"])

    summary = usage_ledger.summarize_query(query)
    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = min(
        max(
            1,
            request.args.get("per_page", REPORT_DEFAULT_PER_PAGE, type=int)
            or REPORT_DEFAULT_PER_PAGE,
        ),
        REPORT_MAX_PER_PAGE,
    )
    pagination = query.order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    rows = pagination.items
    user_ids = {row.user_id for row in rows if row.user_id is not None}
    workspace_ids = {row.workspace_id for row in rows if row.workspace_id is not None}
    users_by_id = (
        {user.id: user for user in User.query.filter(User.id.in_(user_ids)).all()}
        if user_ids
        else {}
    )
    workspaces_by_id = (
        {
            workspace.id: workspace
            for workspace in Workspace.query.filter(Workspace.id.in_(workspace_ids)).all()
        }
        if workspace_ids
        else {}
    )

    return render_template(
        "admin/usage_report.html",
        days=days,
        scope=scope,
        user_id=user_id,
        workspace_id=workspace_id,
        filters=filters,
        summary=summary,
        rows=rows,
        users_by_id=users_by_id,
        workspaces_by_id=workspaces_by_id,
        pagination=pagination,
        per_page=per_page,
        users=User.query.order_by(User.username, User.id).all(),
        workspaces=Workspace.query.order_by(Workspace.name, Workspace.id).all(),
        available_filters=usage_dashboards.available_filters(days=days, now=current),
        currency=get_currency_config(),
    )


@bp.route("/usage/export.csv")
@login_required
@admin_required
def usage_export():
    """Download the org-wide usage ledger as bounded CSV (admin only)."""
    try:
        days = usage_dashboards.normalize_days(request.args.get("days"))
    except ValueError as exc:
        return error_response(INVALID_INPUT, str(exc))
    text, truncated = usage_dashboards.export_csv(days=days)
    response = Response(text, mimetype="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="usage-org-wide.csv"'
    response.headers["Cache-Control"] = "no-store"
    if truncated:
        response.headers["X-Export-Truncated"] = "true"
    return response


@bp.route("/users/<int:user_id>/sessions/revoke-all", methods=["POST"])
@login_required
@admin_required
def revoke_user_sessions(user_id: int):
    """Revoke every active session for an account and audit actor + reason (#619)."""
    user = db.session.get(User, user_id)
    if user is None:
        return error_response(NOT_FOUND, "User not found.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response(INVALID_INPUT, "A JSON object is required.")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return error_response(INVALID_INPUT, "reason is required.")
    try:
        revoked = user_sessions.revoke_all_for_user(
            user,
            actor=current_user,
            reason=reason,
        )
    except user_sessions.SessionError as exc:
        db.session.rollback()
        return error_response(INVALID_INPUT, str(exc))
    return jsonify({"user_id": user.id, "revoked": revoked})


@bp.route("/access-review")
@login_required
@admin_required
def access_review():
    """Who can access each workspace, with last activity and stale flags (#610)."""
    report_format = (request.args.get("format") or "json").strip().lower()
    if report_format not in ("json", "csv"):
        return error_response(INVALID_INPUT, "format must be json or csv")

    rows, truncated = access_review_service.build_rows()
    if report_format == "csv":
        response = Response(access_review_service.to_csv(rows), mimetype="text/csv")
        response.headers["Content-Disposition"] = 'attachment; filename="access-review.csv"'
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Export-Truncated"] = "1" if truncated else "0"
        return response

    return jsonify(
        {
            "items": rows,
            "total": len(rows),
            "truncated": truncated,
            "stale_days": access_review_service.DEFAULT_STALE_DAYS,
        }
    )
