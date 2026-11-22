"""Webhook subscription pages and delivery-log API (#672).

Delivery logs are readable only by the owner of the subscription's workspace.
Every response comes from :mod:`app.services.webhook_deliveries`, which never
exposes a subscription secret or a response body.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

from flask import abort, jsonify, render_template, request
from flask_login import current_user, login_required

from app.api.error_catalog import INVALID_INPUT, NOT_FOUND, error_response
from app.extensions import db
from app.models import WebhookSubscription, Workspace
from app.services import job_queue, siem, webhook_dead_letters, webhook_deliveries
from app.webhooks import bp


def _get_subscription(subscription_id: int) -> WebhookSubscription:
    """Return the subscription when the current user owns its workspace."""
    subscription = WebhookSubscription.query.filter_by(id=subscription_id).first_or_404()
    workspace = db.session.get(Workspace, subscription.workspace_id)
    if workspace is None or workspace.user_id != current_user.id:
        abort(404)
    return subscription


def _get_workspace(workspace_id: int) -> Workspace:
    """Return a workspace owned by the current user, or 404."""
    return Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()


def _get_workspace_subscription(workspace_id: int, subscription_id: int) -> WebhookSubscription:
    _get_workspace(workspace_id)
    return WebhookSubscription.query.filter_by(
        id=subscription_id, workspace_id=workspace_id
    ).first_or_404()


def _validate_webhook_url(url) -> str:
    try:
        return job_queue.validate_webhook_url(str(url or ""))
    except job_queue.JobQueueError as exc:
        raise ValueError(str(exc)) from exc


def _event_type(value) -> str:
    text = str(value or "*").strip() or "*"
    if len(text) > 100:
        raise ValueError("event_type must be 100 characters or fewer")
    return text


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


def _siem_workspace(workspace_id: int) -> Workspace | None:
    """Return the workspace only when the current user owns it."""
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != current_user.id:
        return None
    return workspace


@bp.route("/api/workspaces/<int:workspace_id>/siem", methods=["GET", "PUT", "DELETE"])
@login_required
def api_siem(workspace_id: int):
    """Read, configure, or delete the workspace's SIEM forwarding endpoint."""
    if _siem_workspace(workspace_id) is None:
        return error_response(NOT_FOUND)

    if request.method == "GET":
        endpoint = siem.get_endpoint(workspace_id)
        return jsonify(
            {
                "configured": endpoint is not None,
                "endpoint": endpoint.to_dict() if endpoint is not None else None,
            }
        )

    if request.method == "DELETE":
        try:
            siem.delete_endpoint(workspace_id, actor=current_user)
        except LookupError:
            db.session.rollback()
            return error_response(NOT_FOUND)
        return jsonify({"deleted": True})

    data = request.get_json(silent=True) or {}
    created = siem.get_endpoint(workspace_id) is None
    try:
        endpoint, secret = siem.configure_endpoint(
            workspace_id,
            url=data.get("url"),
            secret=data.get("secret"),
            is_active=data.get("is_active"),
            actor=current_user,
        )
    except (ValueError, LookupError, PermissionError) as exc:
        db.session.rollback()
        return error_response(INVALID_INPUT, str(exc))

    payload = endpoint.to_dict()
    if secret:
        payload["secret"] = secret
    return jsonify(payload), 201 if created else 200


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


@bp.route("/api/workspaces/<int:workspace_id>/subscriptions", methods=["GET", "POST"])
@login_required
def api_subscriptions(workspace_id: int):
    """List or create the workspace's webhook subscriptions (owner only)."""
    _get_workspace(workspace_id)
    if request.method == "GET":
        subscriptions = (
            WebhookSubscription.query.filter_by(workspace_id=workspace_id)
            .order_by(WebhookSubscription.created_at.desc(), WebhookSubscription.id.desc())
            .all()
        )
        return jsonify({"subscriptions": [row.to_dict() for row in subscriptions]})

    data = request.get_json(silent=True) or {}
    events = data.get("events")
    try:
        url = _validate_webhook_url(data.get("url"))
        event_type = _event_type(data.get("event_type"))
        secret = str(data.get("secret") or "").strip() or secrets.token_urlsafe(32)
        if len(secret) > 255:
            raise ValueError("secret must be 255 characters or fewer")
        if events is not None and (not isinstance(events, list) or not events):
            raise ValueError("events must be a non-empty list")
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_webhook_config"}), 400
    subscription = WebhookSubscription(
        workspace_id=workspace_id,
        url=url,
        event_type=event_type,
        is_active=bool(data.get("is_active", True)),
    )
    subscription.set_secret(secret)
    if events is not None:
        try:
            subscription.set_events(events)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": "invalid_webhook_config"}), 400
    db.session.add(subscription)
    db.session.commit()
    payload = subscription.to_dict()
    # The signing secret is returned exactly once, on creation.
    payload["secret"] = secret
    return jsonify(payload), 201


@bp.route(
    "/api/workspaces/<int:workspace_id>/subscriptions/<int:subscription_id>",
    methods=["PATCH", "DELETE"],
)
@login_required
def api_subscription(workspace_id: int, subscription_id: int):
    """Update or delete one subscription (owner only)."""
    subscription = _get_workspace_subscription(workspace_id, subscription_id)
    if request.method == "DELETE":
        db.session.delete(subscription)
        db.session.commit()
        return jsonify({"deleted": True})
    data = request.get_json(silent=True) or {}
    try:
        if "url" in data:
            subscription.url = _validate_webhook_url(data.get("url"))
        if "events" in data:
            subscription.set_events(data.get("events"))
        elif "event_type" in data:
            subscription.event_type = _event_type(data.get("event_type"))
            subscription.set_events([subscription.event_type])
        if "secret" in data:
            secret = str(data.get("secret") or "").strip()
            if not secret or len(secret) > 255:
                raise ValueError("secret must be 1-255 characters")
            subscription.set_secret(secret)
        if "is_active" in data:
            subscription.is_active = bool(data["is_active"])
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": "invalid_webhook_config"}), 400
    db.session.commit()
    return jsonify(subscription.to_dict())


@bp.route(
    "/api/workspaces/<int:workspace_id>/subscriptions/<int:subscription_id>/dead-letters",
    methods=["GET"],
)
@login_required
def api_dead_letters(workspace_id: int, subscription_id: int):
    """List retained permanently failed deliveries (owner only)."""
    subscription = _get_workspace_subscription(workspace_id, subscription_id)
    include_replayed = request.args.get("include_replayed") == "1"
    rows = webhook_dead_letters.list_dead_letters(subscription, include_replayed=include_replayed)
    return jsonify({"dead_letters": [row.to_dict() for row in rows]})


@bp.route(
    "/api/workspaces/<int:workspace_id>/subscriptions/<int:subscription_id>/dead-letters/replay",
    methods=["POST"],
)
@login_required
def api_replay_dead_letters(workspace_id: int, subscription_id: int):
    """Replay a bounded batch of dead letters (owner only)."""
    subscription = _get_workspace_subscription(workspace_id, subscription_id)
    data = request.get_json(silent=True) or {}
    try:
        result = webhook_dead_letters.replay(subscription, data.get("ids"), actor=current_user)
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_replay_request"}), 400
    return jsonify(result)


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
