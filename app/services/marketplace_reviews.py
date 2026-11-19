"""Marketplace ratings and reviews service (#692).

Rules enforced here (the routes are thin wrappers):

* a user must have the plugin installed in a workspace they own or actively
  belong to before rating it;
* one review per user per plugin, enforced by a unique constraint;
* the review author may re-rate/edit their own review;
* any signed-in user may report a review once with a reason;
* the plugin publisher or a platform admin may post an owner response.

Failures raise typed :class:`ReviewError` subclasses carrying a stable code,
HTTP status and UI-safe message.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Plugin, PluginInstallation, PluginReview, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE
from app.services.plugin_publishers import publisher_id_for_plugin

MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 20
MAX_TITLE = 200
MAX_BODY = 4000
MAX_REPORT_REASON = 2000


class ReviewError(ValueError):
    """A review request failed with a stable, UI-safe error."""

    code = "invalid"
    status = 400
    user_message = "The review request is invalid."


class ReviewValidationError(ReviewError):
    """A rating or text field is malformed."""


class ReviewPermissionError(ReviewError):
    """The caller may not perform this review action."""

    code = "forbidden"
    status = 403
    user_message = "You do not have permission to perform this review action."


class ReviewNotFoundError(ReviewError):
    """The plugin or review does not exist."""

    code = "not_found"
    status = 404
    user_message = "The requested review was not found."


class ReviewDuplicateError(ReviewError):
    """The user already reviewed this plugin."""

    code = "duplicate_review"
    status = 409
    user_message = "You have already reviewed this plugin. Edit your existing review instead."


class ReviewInstallRequiredError(ReviewError):
    """Rating requires the plugin to be installed for the caller."""

    code = "install_required"
    status = 403
    user_message = "Install this plugin in one of your workspaces before rating it."


class ReviewAlreadyReportedError(ReviewError):
    """The review has already been reported."""

    code = "already_reported"
    status = 409
    user_message = "This review has already been reported."


def review_error_payload(exc: ReviewError) -> dict[str, str]:
    """Return the stable JSON error shape for a review failure."""
    return {"error": exc.user_message, "code": exc.code}


def user_has_installed(user_id: int, plugin_id: str) -> bool:
    """Return ``True`` when the user owns or actively belongs to a workspace
    that has ``plugin_id`` installed."""
    owned = (
        PluginInstallation.query.join(Workspace, PluginInstallation.workspace_id == Workspace.id)
        .filter(
            PluginInstallation.plugin_id == plugin_id,
            Workspace.user_id == user_id,
        )
        .first()
    )
    if owned is not None:
        return True
    member = (
        PluginInstallation.query.join(
            WorkspaceMember, PluginInstallation.workspace_id == WorkspaceMember.workspace_id
        )
        .filter(
            PluginInstallation.plugin_id == plugin_id,
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.status == STATUS_ACTIVE,
        )
        .first()
    )
    return member is not None


def _validate_rating(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewValidationError("rating must be an integer from 1 to 5")
    if value < 1 or value > 5:
        raise ReviewValidationError("rating must be an integer from 1 to 5")
    return value


def _validate_text(value, *, label: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewValidationError(f"{label} must be text")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise ReviewValidationError(f"{label} must be at most {limit} characters")
    return cleaned


def _get_plugin(plugin_id: str) -> Plugin:
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        raise ReviewNotFoundError(f"Plugin {plugin_id!r} was not found.")
    return plugin


def serialize_review(review: PluginReview) -> dict:
    """Serialize a review with its author name."""
    return review.to_dict()


def list_reviews(plugin_id: str, *, page: int = 1, limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """Return paginated reviews plus average and star distribution."""
    page = max(1, page)
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    query = PluginReview.query.filter_by(plugin_id=plugin_id).order_by(
        PluginReview.created_at.desc(), PluginReview.id.desc()
    )
    total = query.count()
    pages = (total + limit - 1) // limit if total else 0
    rows = query.offset((page - 1) * limit).limit(limit).all()
    ratings = [
        rating
        for (rating,) in PluginReview.query.with_entities(PluginReview.rating)
        .filter_by(plugin_id=plugin_id)
        .all()
    ]
    distribution = {str(star): ratings.count(star) for star in range(5, 0, -1)}
    return {
        "plugin_id": plugin_id,
        "reviews": [serialize_review(review) for review in rows],
        "summary": {
            "count": len(ratings),
            "average": round(sum(ratings) / len(ratings), 2) if ratings else None,
            "distribution": distribution,
        },
        "pagination": {
            "page": page,
            "limit": limit,
            "max_limit": MAX_PAGE_LIMIT,
            "total": total,
            "pages": pages,
            "has_next": page < pages,
            "has_prev": page > 1,
        },
    }


def create_review(user, plugin_id: str, data: dict) -> PluginReview:
    """Create the caller's review for an installed plugin.

    Raises:
        ReviewInstallRequiredError: If the caller has no installation.
        ReviewDuplicateError: If the caller already reviewed this plugin.
        ReviewValidationError: If the rating or text is invalid.
    """
    _get_plugin(plugin_id)
    if not user_has_installed(user.id, plugin_id):
        raise ReviewInstallRequiredError
    if PluginReview.query.filter_by(plugin_id=plugin_id, user_id=user.id).first():
        raise ReviewDuplicateError

    review = PluginReview(
        plugin_id=plugin_id,
        user_id=user.id,
        rating=_validate_rating(data.get("rating")),
        title=_validate_text(data.get("title"), label="title", limit=MAX_TITLE),
        body=_validate_text(data.get("body"), label="body", limit=MAX_BODY),
    )
    db.session.add(review)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ReviewDuplicateError from None
    return review


def _get_review(review_id: int) -> PluginReview:
    review = db.session.get(PluginReview, review_id)
    if review is None:
        raise ReviewNotFoundError
    return review


def update_review(user, review_id: int, data: dict) -> PluginReview:
    """Re-rate or edit the caller's own review."""
    review = _get_review(review_id)
    if review.user_id != user.id:
        raise ReviewPermissionError
    if "rating" in data:
        review.rating = _validate_rating(data.get("rating"))
    if "title" in data:
        review.title = _validate_text(data.get("title"), label="title", limit=MAX_TITLE)
    if "body" in data:
        review.body = _validate_text(data.get("body"), label="body", limit=MAX_BODY)
    db.session.commit()
    return review


def report_review(user, review_id: int, reason) -> PluginReview:
    """Report a review once with a short reason."""
    review = _get_review(review_id)
    if review.reported_at is not None:
        raise ReviewAlreadyReportedError
    cleaned = _validate_text(reason, label="report reason", limit=MAX_REPORT_REASON)
    if cleaned is None:
        raise ReviewValidationError("report reason is required")
    review.reported_at = datetime.now(UTC)
    review.report_reason = cleaned
    db.session.commit()
    return review


def can_respond(plugin: Plugin, user) -> bool:
    """Return ``True`` for platform admins and the plugin's publisher."""
    if getattr(user, "is_admin", False):
        return True
    username = (getattr(user, "username", "") or "").strip().lower()
    if not username:
        return False
    publisher = publisher_id_for_plugin(plugin)
    author = (plugin.author or "").strip().lower()
    return username in {publisher, author}


def respond_to_review(user, review_id: int, response) -> PluginReview:
    """Post or replace the owner response for a review."""
    review = _get_review(review_id)
    plugin = _get_plugin(review.plugin_id)
    if not can_respond(plugin, user):
        raise ReviewPermissionError
    cleaned = _validate_text(response, label="response", limit=MAX_BODY)
    if cleaned is None:
        raise ReviewValidationError("response is required")
    review.owner_response = cleaned
    review.owner_response_at = datetime.now(UTC)
    review.owner_response_by_id = user.id
    db.session.commit()
    return review


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "ReviewAlreadyReportedError",
    "ReviewDuplicateError",
    "ReviewError",
    "ReviewInstallRequiredError",
    "ReviewNotFoundError",
    "ReviewPermissionError",
    "ReviewValidationError",
    "can_respond",
    "create_review",
    "list_reviews",
    "report_review",
    "respond_to_review",
    "review_error_payload",
    "serialize_review",
    "update_review",
    "user_has_installed",
]
