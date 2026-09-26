"""Admin routes: the audit log viewer (issue #34).

The audit log is append-only: this blueprint only ever *reads* entries. There
is deliberately no edit or delete route, for admins or anyone else.
"""

from __future__ import annotations

from functools import wraps

from flask import jsonify, request
from flask_login import current_user, login_required

from app.admin import bp
from app.models import AuditLog, User

#: Hard cap on page size so an admin cannot force an unbounded scan.
MAX_PER_PAGE = 200


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
