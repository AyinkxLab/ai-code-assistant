"""Bounded audit log export as JSON or CSV (#607).

Exports are read-only projections of :class:`~app.models.audit_log.AuditLog`
rows. Only fields classified at ``internal`` or below are emitted, which drops
the actor username (``pii``) and the metadata blob (``confidential``). The
export therefore never contains secrets, prompt content, emails, or other
request context.

Rows are streamed in fixed-size batches so a large export does not load the
whole result set into memory. ``limit`` is an explicit, hard count cap.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime, time, timedelta

from app.models import AuditLog, User
from app.services.classification import INTERNAL, export_record, redact

FORMAT_JSON = "json"
FORMAT_CSV = "csv"
FORMATS = (FORMAT_JSON, FORMAT_CSV)

DEFAULT_LIMIT = 10_000
MAX_LIMIT = 50_000
STREAM_BATCH_SIZE = 500

#: The projection is fixed so a future model column cannot leak into an export
#: merely because it was added to ``to_dict``.
EXPORT_FIELDS = (
    "id",
    "workspace_id",
    "user_id",
    "action",
    "target_type",
    "target_id",
    "created_at",
    "prev_hash",
    "record_hash",
)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise ValueError(f"{field} must use YYYY-MM-DD.") from None


def _parse_limit(value) -> int:
    if value is None or value == "":
        return DEFAULT_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer.") from None
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}.")
    return limit


def build_query(
    *,
    actor: str | None = None,
    action: str | None = None,
    start: str | None = None,
    end: str | None = None,
    resource_type: str | None = None,
    resource_id=None,
    limit=None,
):
    """Return ``(query, limit)`` for the validated filters.

    ``actor`` is an exact username; an unknown name deliberately yields an
    empty result rather than revealing whether the account exists. Dates are
    ``YYYY-MM-DD`` and both bounds are inclusive. Raises ``ValueError`` for any
    malformed filter so the route can return a structured error.
    """
    query = AuditLog.query
    actor = (actor or "").strip()
    action = (action or "").strip()
    resource_type = (resource_type or "").strip()

    if actor:
        user = User.query.filter_by(username=actor).first()
        query = query.filter(AuditLog.user_id == (user.id if user else -1))
    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.target_type == resource_type)
    if resource_id is not None and resource_id != "":
        try:
            parsed_id = int(resource_id)
        except (TypeError, ValueError):
            raise ValueError("resource_id must be an integer.") from None
        query = query.filter(AuditLog.target_id == parsed_id)

    start_date = _parse_date(start, "start") if start else None
    end_date = _parse_date(end, "end") if end else None
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start must not be after end.")
    if start_date is not None:
        query = query.filter(
            AuditLog.created_at >= datetime.combine(start_date, time.min, tzinfo=UTC)
        )
    if end_date is not None:
        next_day = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
        query = query.filter(AuditLog.created_at < next_day)

    return query.order_by(AuditLog.created_at.asc(), AuditLog.id.asc()), _parse_limit(limit)


def export_row(entry: AuditLog) -> dict:
    """Project one entry to the whitelisted, classification-filtered export row."""
    row = {
        "id": entry.id,
        "workspace_id": entry.workspace_id,
        "user_id": entry.user_id,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "prev_hash": entry.prev_hash,
        "record_hash": entry.record_hash,
    }
    return redact(export_record(row, max_level=INTERNAL), max_level=INTERNAL)


def iter_rows(query, limit: int):
    """Yield export rows in bounded batches, stopping at ``limit``."""
    for entry in query.limit(limit).yield_per(STREAM_BATCH_SIZE):
        yield export_row(entry)


def stream_json(query, limit: int):
    """Yield a valid JSON array one row at a time."""
    yield "["
    first = True
    for row in iter_rows(query, limit):
        if not first:
            yield ","
        first = False
        yield json.dumps(row, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    yield "]\n"


def stream_csv(query, limit: int):
    """Yield a CSV document with a stable header and streamed body."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_FIELDS), extrasaction="ignore")
    writer.writeheader()
    yield buffer.getvalue()
    for row in iter_rows(query, limit):
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(row)
        yield buffer.getvalue()


__all__ = [
    "DEFAULT_LIMIT",
    "EXPORT_FIELDS",
    "FORMATS",
    "FORMAT_CSV",
    "FORMAT_JSON",
    "MAX_LIMIT",
    "build_query",
    "export_row",
    "iter_rows",
    "stream_csv",
    "stream_json",
]
