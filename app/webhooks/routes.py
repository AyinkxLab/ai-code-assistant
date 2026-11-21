"""Webhook subscription pages and delivery-log API (#672).

Delivery logs are readable only by the owner of the subscription's workspace.
Every response comes from :mod:`app.services.webhook_deliveries`, which never
exposes a subscription secret or a response body.
"""

from __future__ import annotations

from urllib.parse import urlencode

from flask import abort, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import WebhookSubscription, Workspace
from app.services import webhook_deliveries
from app.webhooks import bp


def _get_subscription(subscription_id: int) -> WebhookSubscription:
    """Return the subscription when the current user owns its workspace."""
    subscription = WebhookSubscription.query.filter_by(id=subscription_id).first_or_404()
    workspace = db.session.get(Workspace, subscription.workspace_id)
    if workspace is None or workspace.user_id != current_user.id:
        abort(404)
    return subscription


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _filters() -> tuple[str | None, object, object, int, int]:
    """Validate the shared status/date/pagination query parameters."""
    status = (request.args.get("status") or "").strip() or None
    start_raw = (request.args.get("start") or "").strip()
    end_raw = (request.args.get("end") or "").strip()
    start = webhook_deliveries.parse_date(start_raw) if start_raw else None
    end = webhook_deliveries.parse_date(end_raw, end_of_day=True) if end_raw else None
    page = max(1, _int_arg("page", 1))
    per_page = max(1, _int_arg("per_page", webhook_deliveries.DEFAULT_PAGE_SIZE))
    return status, start, end, page, per_page


def _filter_query() -> str:
    """Return an HTML-safe suffix that preserves filters across page links."""
    params = {key: value for key, value in request.args.items() if key in FILTER_KEYS}
    encoded = urlencode(params)
    return f"&{encoded}" if encoded else ""


FILTER_KEYS = frozenset({"status", "start", "end", "per_page"})


@bp.route("/")
@login_required
def index():
    """List the current user's webhook subscriptions."""
    workspace_ids = [
        workspace.id for workspace in Workspace.query.filter_by(user_id=current_user.id).all()
    ]
    subscriptions = []
    if workspace_ids:
        subscriptions = (
            WebhookSubscription.query.filter(WebhookSubscription.workspace_id.in_(workspace_ids))
            .order_by(WebhookSubscription.created_at.desc(), WebhookSubscription.id.desc())
            .all()
        )
    return render_template("webhooks/index.html", subscriptions=subscriptions)


@bp.route("/subscriptions/<int:subscription_id>/deliveries")
@login_required
def deliveries_page(subscription_id: int):
    """Render the paginated, filterable delivery-log page."""
    subscription = _get_subscription(subscription_id)
    try:
        status, start, end, page, per_page = _filters()
        result = webhook_deliveries.list_deliveries(
            subscription,
            status=status,
            start=start,
            end=end,
            page=page,
            per_page=per_page,
        )
    except ValueError as exc:
        return (
            render_template(
                "webhooks/deliveries.html",
                subscription=subscription,
                result=None,
                error=str(exc),
                filter_query="",
            ),
            400,
        )
    return render_template(
        "webhooks/deliveries.html",
        subscription=subscription,
        result=result,
        error=None,
        filter_query=_filter_query(),
    )


@bp.route("/api/subscriptions/<int:subscription_id>/deliveries")
@login_required
def api_deliveries(subscription_id: int):
    """Return one page of delivery attempts as JSON."""
    subscription = _get_subscription(subscription_id)
    try:
        status, start, end, page, per_page = _filters()
        result = webhook_deliveries.list_deliveries(
            subscription,
            status=status,
            start=start,
            end=end,
            page=page,
            per_page=per_page,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_filter"}), 400
    return jsonify(result)
