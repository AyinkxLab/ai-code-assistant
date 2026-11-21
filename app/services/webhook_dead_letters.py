"""Dead-letter storage and bounded replay for webhooks (#671).

Permanently failed deliveries can be retained in a dead-letter queue and
replayed by the workspace owner. Replay batches are bounded, each payload has a
retention window, duplicates are skipped, and the actor is recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models.webhook import WebhookDeadLetter

MAX_BATCH = 50
MAX_PAYLOAD_BYTES = 64 * 1024
RETENTION_DAYS = 7
MAX_ERROR_CHARS = 500


def record_failure(
    subscription,
    *,
    event_type: str,
    payload: dict,
    attempts: int = 1,
    response_code: int | None = None,
    error: str | None = None,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
    commit: bool = True,
) -> WebhookDeadLetter:
    """Persist one permanently failed delivery for later replay."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    try:
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload is not JSON-serializable") from exc
    if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload is too large for the dead-letter queue")
    now = now or datetime.now(UTC)
    row = WebhookDeadLetter(
        subscription_id=subscription.id,
        event_type=(event_type or "unknown")[:100],
        payload=serialized,
        attempts=attempts,
        response_code=response_code,
        error=(error[:MAX_ERROR_CHARS] if error else None),
        expires_at=now + timedelta(days=max(0, retention_days)),
        created_at=now,
    )
    db.session.add(row)
    if commit:
        db.session.commit()
    return row


def list_dead_letters(
    subscription, *, include_replayed: bool = False, limit: int = 100
) -> list[WebhookDeadLetter]:
    """List dead letters for a subscription, newest first."""
    query = WebhookDeadLetter.query.filter_by(subscription_id=subscription.id)
    if not include_replayed:
        query = query.filter(WebhookDeadLetter.replayed_at.is_(None))
    return (
        query.order_by(WebhookDeadLetter.created_at.desc(), WebhookDeadLetter.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )


def replay(
    subscription,
    ids,
    *,
    actor,
    now: datetime | None = None,
    enqueue=None,
) -> dict:
    """Replay up to :data:`MAX_BATCH` dead letters for ``subscription``."""
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids must be a non-empty list")
    if len(ids) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} deliveries can be replayed at once")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids):
        raise ValueError("ids must contain positive integers")
    unique_ids = list(dict.fromkeys(ids))
    now = now or datetime.now(UTC)

    if enqueue is None:
        from app.services import job_queue

        enqueue = job_queue.enqueue

    rows = WebhookDeadLetter.query.filter(
        WebhookDeadLetter.subscription_id == subscription.id,
        WebhookDeadLetter.id.in_(unique_ids),
    ).all()
    by_id = {row.id: row for row in rows}
    replayed = 0
    duplicates = 0
    expired = 0
    failed = 0
    missing = [value for value in unique_ids if value not in by_id]

    for value in unique_ids:
        row = by_id.get(value)
        if row is None:
            continue
        if row.replayed_at is not None:
            duplicates += 1
            continue
        expires_at = row.expires_at
        if expires_at is not None:
            # SQLite returns naive datetimes; normalize before comparing.
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                expired += 1
                continue
        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError):
            failed += 1
            continue
        try:
            enqueue("webhook.deliver", {"url": subscription.url, "payload": payload})
        except Exception:  # a replay failure must not abort the batch
            failed += 1
            continue
        row.replayed_at = now
        row.replayed_by_id = getattr(actor, "id", None)
        replayed += 1

    db.session.commit()
    return {
        "replayed": replayed,
        "duplicates": duplicates,
        "expired": expired,
        "failed": failed,
        "missing": missing,
    }


__all__ = [
    "MAX_BATCH",
    "MAX_PAYLOAD_BYTES",
    "RETENTION_DAYS",
    "list_dead_letters",
    "record_failure",
    "replay",
]
