"""Usage and cost dashboards (Phase 10, issue #368).

Dashboards are computed **only** from stored :class:`UsageRecord` rows. They
never read prompts, fabricate spend, or fall back to message token counts. Every
breakdown is deterministic: rows are grouped by label and sorted by label, and
daily buckets are sorted by date.

The same aggregation backs the per-user page, the per-workspace view, the admin
org-wide view, and the bounded CSV export.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from app.models import UsageRecord
from app.services import usage_ledger
from app.services.currency import get_currency_config

#: Default and maximum dashboard window, in days.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365

#: Hard cap on CSV rows and bytes, so an export can never stream unbounded data.
MAX_CSV_ROWS = 5000
MAX_CSV_BYTES = 2 * 1024 * 1024

CSV_COLUMNS = (
    "date",
    "user_id",
    "workspace_id",
    "provider",
    "model",
    "source",
    "status",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "tokens_estimated",
    "cost_estimate",
    "currency",
)


def normalize_days(value, default: int = DEFAULT_WINDOW_DAYS) -> int:
    """Coerce a query-string window into ``1..MAX_WINDOW_DAYS``.

    Raises:
        ValueError: If the value is not an integer or is out of range.
    """
    if value is None or value == "":
        return default
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be an integer.") from exc
    if days < 1 or days > MAX_WINDOW_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_WINDOW_DAYS}.")
    return days


def _window_start(days: int, now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    return current - timedelta(days=days)


def normalize_filter(value) -> str | None:
    """Trim a query-string filter; blank means "no filter"."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def query_records(
    *,
    user_id: int | None = None,
    workspace_id: int | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    source: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> list[UsageRecord]:
    """Return ledger rows inside the window, in deterministic order."""
    since = _window_start(days, now)
    query = UsageRecord.query.filter(UsageRecord.created_at >= since)
    if user_id is not None:
        query = query.filter(UsageRecord.user_id == user_id)
    if workspace_id is not None:
        query = query.filter(UsageRecord.workspace_id == workspace_id)
    if source:
        query = query.filter(UsageRecord.source == source)
    if provider:
        query = query.filter(UsageRecord.provider == provider)
    if model:
        query = query.filter(UsageRecord.model == model)
    return query.order_by(UsageRecord.created_at, UsageRecord.id).all()


def available_filters(
    *,
    user_id: int | None = None,
    workspace_id: int | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict:
    """Distinct filter values in the current scope/window (before filtering)."""
    records = query_records(user_id=user_id, workspace_id=workspace_id, days=days, now=now)

    def distinct(attribute: str) -> list[str]:
        return sorted(
            {
                _label(getattr(record, attribute, None))
                for record in records
                if getattr(record, attribute, None) not in (None, "")
            }
        )

    return {
        "sources": distinct("source"),
        "providers": distinct("provider"),
        "models": distinct("model"),
    }


def _label(value, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _group(records: list[UsageRecord], attribute: str, label: str) -> list[dict]:
    """Group rows by ``attribute`` and summarize each group deterministically."""
    buckets: dict[str, list[UsageRecord]] = {}
    for record in records:
        key = _label(getattr(record, attribute, None))
        buckets.setdefault(key, []).append(record)
    rows = []
    for key in sorted(buckets):
        rows.append({label: key, **usage_ledger.summarize_records(buckets[key])})
    return rows


def _daily(records: list[UsageRecord]) -> list[dict]:
    """Build a per-day series, omitting days with no usage."""
    buckets: dict[str, list[UsageRecord]] = {}
    for record in records:
        created = getattr(record, "created_at", None)
        if created is None:
            continue
        day = created.date().isoformat()
        buckets.setdefault(day, []).append(record)
    return [
        {"date": day, **usage_ledger.summarize_records(buckets[day])} for day in sorted(buckets)
    ]


def _payload(records: list[UsageRecord]) -> dict:
    by_source = usage_ledger.breakdown_by_source(records)
    return {
        "currency": get_currency_config(),
        "cost_is_estimate": True,
        "totals": usage_ledger.summarize_records(records),
        "by_source": [{"source": source, **stats} for source, stats in by_source.items()],
        "by_provider": _group(records, "provider", "provider"),
        "by_model": _group(records, "model", "model"),
        "daily": _daily(records),
    }


def dashboard(
    *,
    user_id: int | None = None,
    workspace_id: int | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    source: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict:
    """Compute a dashboard payload from stored usage rows.

    The optional ``source``/``provider``/``model`` filters are applied to every
    panel (totals, breakdowns and the daily series) so the whole page describes
    the same slice. Empty scopes return zero totals and empty breakdowns; the
    shape is stable so templates and clients can render empty states without
    special cases.
    """
    filters = {
        "source": normalize_filter(source),
        "provider": normalize_filter(provider),
        "model": normalize_filter(model),
    }
    records = query_records(
        user_id=user_id,
        workspace_id=workspace_id,
        days=days,
        now=now,
        **filters,
    )
    payload = _payload(records)
    payload["filters"] = filters
    payload["available_filters"] = available_filters(
        user_id=user_id, workspace_id=workspace_id, days=days, now=now
    )
    payload["scope"] = {
        "type": "workspace" if workspace_id is not None else "user",
        "user_id": user_id,
        "workspace_id": workspace_id,
        "days": days,
        "filters": filters,
    }
    return payload


def admin_dashboard(*, days: int = DEFAULT_WINDOW_DAYS, now: datetime | None = None) -> dict:
    """Compute the org-wide dashboard used by ``app/admin``."""
    records = query_records(days=days, now=now)
    payload = _payload(records)
    payload["scope"] = {"type": "admin", "user_id": None, "workspace_id": None, "days": days}
    payload["by_user"] = _group(records, "user_id", "user_id")
    return payload


def export_csv(
    *,
    user_id: int | None = None,
    workspace_id: int | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    source: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_rows: int = MAX_CSV_ROWS,
    max_bytes: int = MAX_CSV_BYTES,
) -> tuple[str, bool]:
    """Render a bounded CSV export and return ``(text, truncated)``.

    The row and byte caps are hard limits. Once either is reached the remaining
    rows are omitted and ``truncated`` is ``True`` so the caller can advertise
    that fact via a response header. The source/provider/model filters match
    :func:`dashboard` so the export is the same slice as the page.
    """
    records = query_records(
        user_id=user_id,
        workspace_id=workspace_id,
        days=days,
        now=now,
        source=normalize_filter(source),
        provider=normalize_filter(provider),
        model=normalize_filter(model),
    )
    currency = get_currency_config()["code"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    written = 0
    truncated = False
    for written, record in enumerate(records):
        if written >= max_rows or buffer.tell() >= max_bytes:
            truncated = True
            break
        created = getattr(record, "created_at", None)
        writer.writerow(
            {
                "date": created.date().isoformat() if created else "",
                "user_id": record.user_id if record.user_id is not None else "",
                "workspace_id": record.workspace_id if record.workspace_id is not None else "",
                "provider": _label(record.provider, ""),
                "model": _label(record.model, ""),
                "source": usage_ledger.normalize_source(record.source)
                or usage_ledger.UNKNOWN_SOURCE,
                "status": _label(record.status, ""),
                "prompt_tokens": int(record.prompt_tokens or 0),
                "completion_tokens": int(record.completion_tokens or 0),
                "total_tokens": int(record.total_tokens or 0),
                "tokens_estimated": bool(record.tokens_estimated),
                "cost_estimate": "" if record.cost_estimate is None else record.cost_estimate,
                "currency": currency,
            }
        )

    text = buffer.getvalue()
    if len(text.encode("utf-8")) > max_bytes:
        # A single wide row can still exceed the byte cap; trim complete lines.
        while text and len(text.encode("utf-8")) > max_bytes:
            text = text.rsplit("\n", 1)[0] + "\n"
            truncated = True
    return text, truncated


__all__ = [
    "CSV_COLUMNS",
    "DEFAULT_WINDOW_DAYS",
    "MAX_CSV_BYTES",
    "MAX_CSV_ROWS",
    "MAX_WINDOW_DAYS",
    "admin_dashboard",
    "available_filters",
    "dashboard",
    "export_csv",
    "normalize_days",
    "normalize_filter",
    "query_records",
]
