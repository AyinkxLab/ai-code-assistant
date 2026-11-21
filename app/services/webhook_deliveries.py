"""Persist and query webhook delivery attempts (#670, #672).

Writes go through :func:`record_delivery`; reads go through
:func:`list_deliveries`, which applies status and date filters and bounded
pagination. Neither path stores or returns a response body or a subscription
secret.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from datetime import time as dt_time

from app.extensions import db
from app.models.webhook import (
    DELIVERY_STATUSES,
    WebhookDelivery,
    WebhookSubscription,
)

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_ERROR_CHARS = 500


def record_delivery(
    subscription: WebhookSubscription,
    *,
    event_type: str,
    status: str,
    attempt: int = 1,
    response_code: int | None = None,
    latency_ms: float | None = None,
    error: str | None = None,
    commit: bool = True,
) -> WebhookDelivery:
    """Append one delivery attempt for ``subscription``.

    ``error`` is a short label (for example ``HTTP 503``) and is truncated; a
    caller must never pass a response body or secret.
    """
    if status not in DELIVERY_STATUSES:
        raise ValueError(f"unsupported delivery status: {status}")
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    row = WebhookDelivery(
        subscription_id=subscription.id,
        event_type=(event_type or "unknown")[:100],
        attempt=attempt,
        status=status,
        response_code=response_code,
        latency_ms=int(latency_ms) if latency_ms is not None else None,
        error=(error[:MAX_ERROR_CHARS] if error else None),
    )
    db.session.add(row)
    if commit:
        db.session.commit()
    return row


def parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse an ISO ``YYYY-MM-DD`` date into a UTC datetime boundary."""
    try:
        day = date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise ValueError("dates must be ISO 8601 (YYYY-MM-DD)") from None
    boundary = dt_time.max if end_of_day else dt_time.min
    return datetime.combine(day, boundary, tzinfo=UTC)


def list_deliveries(
    subscription: WebhookSubscription,
    *,
    status: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Return a filtered, paginated page of deliveries for ``subscription``."""
    query = WebhookDelivery.query.filter_by(subscription_id=subscription.id)
    if status:
        if status not in DELIVERY_STATUSES:
            raise ValueError(f"unsupported delivery status: {status}")
        query = query.filter_by(status=status)
    if start is not None:
        query = query.filter(WebhookDelivery.created_at >= start)
    if end is not None:
        query = query.filter(WebhookDelivery.created_at <= end)

    page = max(1, int(page))
    per_page = max(1, min(int(per_page), MAX_PAGE_SIZE))
    total = query.count()
    rows = (
        query.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "items": [row.to_dict() for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "has_next": page * per_page < total,
        "has_prev": page > 1,
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "list_deliveries",
    "parse_date",
    "record_delivery",
]
