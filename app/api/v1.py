"""Stable public API v1 resources (#646).

    GET /api/v1/projects              list the caller's projects
    GET /api/v1/projects/<id>         one project
    GET /api/v1/reviews               list the caller's reviews
    GET /api/v1/reviews/<id>          one review
    GET /api/v1/usage                 bounded usage-ledger page

Authorization matches the web UI exactly: projects and reviews are scoped to
``current_user.id`` and another user's row is reported as ``404`` (never as a
403 existence oracle). Usage is always scoped to the authenticated user.
Errors use the shared code catalog via :func:`error_response`.
"""

from __future__ import annotations

import functools

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app.api.error_catalog import INVALID_INPUT, NOT_FOUND, UNAUTHORIZED, error_response
from app.models import Project, Review
from app.services import pagination, usage_ledger

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        return view(*args, **kwargs)

    return wrapper


def _page_request():
    """Validate page/limit/cursor, returning ``(request, error_response)``."""
    try:
        return pagination.parse_page_request(request.args), None
    except pagination.PaginationError as exc:
        return None, error_response(INVALID_INPUT, str(exc))


def _cursor_id(page) -> int | None:
    """Return the opaque cursor's ``id``, or ``None`` when absent."""
    if page.cursor is None:
        return None
    if set(page.cursor) != {"id"}:
        raise pagination.PaginationError("Invalid cursor.")
    value = page.cursor["id"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise pagination.PaginationError("Invalid cursor.")
    return value


def _paginate(query, model, page):
    """Return ``(rows, total, next_cursor)`` for a stable id-ordered list."""
    total = query.count()
    cursor_id = _cursor_id(page)
    query = query.order_by(model.id.desc())
    if cursor_id is not None:
        query = query.filter(model.id < cursor_id)
    else:
        query = query.offset((page.page - 1) * page.limit)
    rows = query.limit(page.limit + 1).all()
    has_more = len(rows) > page.limit
    rows = rows[: page.limit]
    next_cursor = pagination.encode_cursor({"id": rows[-1].id}) if has_more and rows else None
    return rows, total, next_cursor


def _list_payload(items, total, page, next_cursor):
    return {
        "items": items,
        "total": total,
        "page": page.page,
        "limit": page.limit,
        "next_cursor": next_cursor,
    }


def _optional_int(name: str):
    """Parse an optional integer query argument, or return ``(None, error)``."""
    raw = request.args.get(name)
    if raw in (None, ""):
        return None, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, error_response(INVALID_INPUT, f"{name} must be an integer.")


@bp.route("/projects", methods=["GET"])
@_login_required
def list_projects():
    """List the caller's projects, newest id first."""
    page, error = _page_request()
    if error is not None:
        return error

    workspace_id, error = _optional_int("workspace_id")
    if error is not None:
        return error
    query = Project.query.filter_by(user_id=current_user.id)
    if workspace_id is not None:
        query = query.filter_by(workspace_id=workspace_id)
    try:
        rows, total, next_cursor = _paginate(query, Project, page)
    except pagination.PaginationError as exc:
        return error_response(INVALID_INPUT, str(exc))
    return jsonify(_list_payload([row.to_dict() for row in rows], total, page, next_cursor))


@bp.route("/projects/<int:project_id>", methods=["GET"])
@_login_required
def get_project(project_id: int):
    """Return one project owned by the caller."""
    project = Project.query.filter_by(id=project_id, user_id=current_user.id).first()
    if project is None:
        return error_response(NOT_FOUND)
    return jsonify(project.to_dict())


@bp.route("/reviews", methods=["GET"])
@_login_required
def list_reviews():
    """List the caller's reviews, newest id first."""
    page, error = _page_request()
    if error is not None:
        return error

    query = Review.query.filter_by(user_id=current_user.id)
    for name in ("source", "kind", "status"):
        value = request.args.get(name)
        if value:
            query = query.filter_by(**{name: value})
    project_id, error = _optional_int("project_id")
    if error is not None:
        return error
    if project_id is not None:
        query = query.filter_by(project_id=project_id)

    try:
        rows, total, next_cursor = _paginate(query, Review, page)
    except pagination.PaginationError as exc:
        return error_response(INVALID_INPUT, str(exc))
    return jsonify(_list_payload([row.to_dict() for row in rows], total, page, next_cursor))


@bp.route("/reviews/<int:review_id>", methods=["GET"])
@_login_required
def get_review(review_id: int):
    """Return one review owned by the caller."""
    review = Review.query.filter_by(id=review_id, user_id=current_user.id).first()
    if review is None:
        return error_response(NOT_FOUND)
    return jsonify(review.to_dict())


@bp.route("/usage", methods=["GET"])
@_login_required
def list_usage():
    """Return a bounded page of the caller's usage-ledger records."""
    page, error = _page_request()
    if error is not None:
        return error
    if page.cursor is not None:
        return error_response(
            INVALID_INPUT, "Cursor pagination is not supported here; use page and limit."
        )

    workspace_id, error = _optional_int("workspace_id")
    if error is not None:
        return error
    project_id, error = _optional_int("project_id")
    if error is not None:
        return error

    payload = usage_ledger.export_payload(
        user_id=current_user.id,
        workspace_id=workspace_id,
        project_id=project_id,
        source=request.args.get("source") or None,
        page=page.page,
        limit=page.limit,
    )
    return jsonify(payload)
