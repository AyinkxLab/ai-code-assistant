"""Admin routes: the audit log viewer (issue #34).

The audit log is append-only: this blueprint only ever *reads* entries. There
is deliberately no edit or delete route, for admins or anyone else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import wraps

from flask import Response, jsonify, render_template, request
from flask_login import current_user, login_required

from app.admin import bp
from app.models import AuditLog, UsageRecord, User, Workspace
from app.services import usage_dashboards, usage_ledger
from app.services.currency import get_currency_config

#: Hard cap on page size so an admin cannot force an unbounded scan.
MAX_PER_PAGE = 200

#: Bounds for the detailed usage report table (#480).
REPORT_DEFAULT_PER_PAGE = 50
REPORT_MAX_PER_PAGE = 100
REPORT_SCOPES = ("all", "user", "workspace")


def admin_required(view):
    """Reject non-admin callers with a 403 JSON error."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required.", "kind": "unauthorized"}), 401
        if not current_user.is_admin:
            return jsonify({"error": "Admin access required.", "kind": "forbidden"}), 403
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
        return jsonify({"error": str(exc), "kind": "invalid"}), 400
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
        return jsonify({"error": str(exc), "kind": "invalid"}), 400

    scope = (request.args.get("scope") or "all").strip().lower()
    if scope not in REPORT_SCOPES:
        return jsonify({"error": "scope must be all, user or workspace.", "kind": "invalid"}), 400

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
        return jsonify({"error": str(exc), "kind": "invalid"}), 400
    text, truncated = usage_dashboards.export_csv(days=days)
    response = Response(text, mimetype="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="usage-org-wide.csv"'
    response.headers["Cache-Control"] = "no-store"
    if truncated:
        response.headers["X-Export-Truncated"] = "true"
    return response
