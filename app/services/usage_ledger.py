"""Central usage ledger service (Phase 10).

The ledger is append-only: callers add :class:`~app.models.UsageRecord` rows and
readers aggregate them. This module keeps the normalisation and aggregation
rules in one place so project chat, project analysis, dashboards and future
features (reviews, tools, budgets) all report the same numbers.

Only metadata is stored. Prompts and completions are never written here.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_

from app.extensions import db
from app.models import Message, UsageRecord
from app.services import pricing

#: Operation families understood by the ledger. Unknown values are grouped
#: under :data:`UNKNOWN_SOURCE` instead of crashing a dashboard.
SOURCE_CHAT = "chat"
SOURCE_ANALYSIS = "analysis"
SOURCE_REVIEW = "review"
SOURCE_TOOLS = "tools"
SOURCE_PROJECT_CHAT = "project_chat"
SOURCES = (
    SOURCE_CHAT,
    SOURCE_ANALYSIS,
    SOURCE_REVIEW,
    SOURCE_TOOLS,
    SOURCE_PROJECT_CHAT,
)
UNKNOWN_SOURCE = "unknown"

#: Default/upper bounds for the historical chat-message backfill. The work is
#: paginated by message id so a large conversation history is never loaded at
#: once.
BACKFILL_DEFAULT_BATCH_SIZE = 500
BACKFILL_MAX_BATCH_SIZE = 5000

#: ``request_id`` prefix used to make backfilled rows idempotent per message.
#: The ledger predates a dedicated ``message_id`` column, so a stable sentinel
#: in the existing correlation field identifies the source message without a
#: schema change. It stays well under the 64-character limit.
BACKFILL_REQUEST_PREFIX = "backfill:message:"

#: Operation outcomes understood by the ledger (see docs/observability.md).
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_DENIED = "denied"

#: Default usage retention window (days) when the app config does not set one.
DEFAULT_RETENTION_DAYS = 365

#: Smallest allowed retention window, so a purge can never target "today".
MIN_RETENTION_DAYS = 1

#: Version of the JSON usage export document. Increment when a field changes
#: meaning or is removed; additive fields do not require a bump.
EXPORT_SCHEMA_VERSION = 1

#: Default and maximum page sizes for the JSON usage export. The export is
#: deliberately bounded even when the ledger is large.
EXPORT_DEFAULT_LIMIT = 100
EXPORT_MAX_LIMIT = 500

#: Bounds for the historical cost recomputation command (#440).
RECOMPUTE_DEFAULT_BATCH_SIZE = 500
RECOMPUTE_MAX_BATCH_SIZE = 1000
RECOMPUTE_DEFAULT_MAX_ROWS = 5000
RECOMPUTE_MAX_ROWS = 100_000

#: Hard row cap for the CSV export. Rows beyond the cap are omitted and the
#: response advertises truncation instead of streaming an unbounded document.
EXPORT_CSV_MAX_ROWS = 5000

#: Stable CSV column order shared by the API export and its tests.
EXPORT_CSV_COLUMNS = (
    "created_at",
    "user_id",
    "workspace_id",
    "project_id",
    "conversation_id",
    "source",
    "cost_center",
    "provider",
    "model",
    "status",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "tokens_estimated",
    "cost_estimate",
    "currency",
)


def normalize_source(source: Any) -> str | None:
    """Return the lowercase source label, or ``None`` when it is missing."""
    value = str(source or "").strip().lower()
    return value[:40] or None


def source_bucket(source: Any) -> str:
    """Group a raw source into a known bucket, falling back to ``unknown``."""
    value = normalize_source(source)
    if value in SOURCES:
        return value
    return UNKNOWN_SOURCE


def _non_negative_int(value, name: str) -> int:
    """Coerce a count to a non-negative integer, rejecting invalid input."""
    if value in (None, ""):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if count < 0:
        raise ValueError(f"{name} must be non-negative.")
    return count


def _optional_non_negative_int(value, name: str) -> int | None:
    """Coerce an optional measurement to a non-negative integer, or ``None``."""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if number < 0:
        raise ValueError(f"{name} must be non-negative.")
    return number


def _optional_text(value, name: str, limit: int) -> str | None:
    """Normalize an optional short string field, or return ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def normalize_usage(prompt_tokens=0, completion_tokens=0, total_tokens=None) -> dict[str, int]:
    """Return validated prompt/completion/total token counts.

    ``total_tokens`` is derived from the two parts when it is omitted. Explicit
    values are validated as well, so a malformed provider payload can never
    write a negative count into the ledger.
    """
    prompt = _non_negative_int(prompt_tokens, "prompt_tokens")
    completion = _non_negative_int(completion_tokens, "completion_tokens")
    total = (
        prompt + completion
        if total_tokens is None
        else _non_negative_int(total_tokens, "total_tokens")
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _object_id(value, attribute: str = "id"):
    if value is None:
        return None
    return getattr(value, attribute, None)


def current_provider_metadata(user=None) -> tuple[str | None, str | None]:
    """Return ``(provider_name, model)`` for the active provider, if resolvable.

    Instrumentation must never break the operation it is recording, so any
    configuration error simply yields ``(None, None)`` and cost stays unknown.
    """
    try:
        from app.services.providers import get_provider

        provider = get_provider(user=user)
        name = getattr(provider, "name", None)
        model = getattr(provider, "model", None)
        if not model:
            models = getattr(provider, "models", ()) or ()
            model = models[0] if models else None
        return name, model
    except Exception:  # pragma: no cover - defensive: never break instrumentation
        return None, None


def estimate_cost(
    provider,
    model,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    workspace_id=None,
) -> float | None:
    """Estimate cost, using workspace overrides when known.

    Returns ``None`` when the provider/model has no price, so an unknown model
    is never silently recorded as free.
    """
    if not provider or not model:
        return None
    return pricing.estimate_cost_usd(
        provider,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        workspace_id=workspace_id,
    )


def record_usage(
    *,
    source: str,
    cost_center: str | None = None,
    prompt_tokens=0,
    completion_tokens=0,
    total_tokens=None,
    user=None,
    user_id=None,
    workspace=None,
    workspace_id=None,
    project=None,
    project_id=None,
    conversation=None,
    conversation_id=None,
    provider: str | None = None,
    model: str | None = None,
    tokens_estimated: bool = True,
    cost_estimate=None,
    request_id: str | None = None,
    latency_ms=None,
    status: str | None = None,
    commit: bool = True,
) -> UsageRecord:
    """Append one usage record and return it.

    Scope objects take precedence over their explicit id counterparts. Cost is
    estimated from the configured price table when the caller does not supply a
    value; an unknown provider/model leaves it as ``None`` rather than zero.

    ``request_id``, ``latency_ms`` and ``status`` are optional correlation and
    performance fields. They default to ``None`` (the operation did not report
    them) and are validated so a malformed provider payload cannot write a
    negative latency or an over-long id into the ledger.
    """
    counts = normalize_usage(prompt_tokens, completion_tokens, total_tokens)
    provider = (str(provider).strip()[:50] or None) if provider else None
    model = (str(model).strip()[:100] or None) if model else None
    cost_center = _optional_text(cost_center, "cost_center", 100)
    request_id = _optional_text(request_id, "request_id", 64)
    latency_ms = _optional_non_negative_int(latency_ms, "latency_ms")
    status = _optional_text(status, "status", 20)
    if status:
        status = status.lower()

    # Scope objects take precedence over their explicit id counterparts.
    resolved_workspace_id = (
        _object_id(workspace)
        or workspace_id
        or _object_id(project, "workspace_id")
        or _object_id(conversation, "workspace_id")
    )
    resolved_user_id = _object_id(user) or user_id
    resolved_project_id = _object_id(project) or project_id
    resolved_conversation_id = _object_id(conversation) or conversation_id

    if cost_estimate is None:
        cost_estimate = estimate_cost(
            provider,
            model,
            counts["prompt_tokens"],
            counts["completion_tokens"],
            workspace_id=resolved_workspace_id,
        )
    else:
        try:
            cost_estimate = float(cost_estimate)
        except (TypeError, ValueError) as exc:
            raise ValueError("cost_estimate must be numeric.") from exc
        if cost_estimate < 0:
            raise ValueError("cost_estimate must be non-negative.")
        cost_estimate = pricing.round_cost(cost_estimate)

    record = UsageRecord(
        user_id=resolved_user_id,
        workspace_id=resolved_workspace_id,
        project_id=resolved_project_id,
        conversation_id=resolved_conversation_id,
        source=normalize_source(source) or UNKNOWN_SOURCE,
        cost_center=cost_center,
        provider=provider,
        model=model,
        prompt_tokens=counts["prompt_tokens"],
        completion_tokens=counts["completion_tokens"],
        total_tokens=counts["total_tokens"],
        tokens_estimated=bool(tokens_estimated),
        cost_estimate=cost_estimate,
        request_id=request_id,
        latency_ms=latency_ms,
        status=status,
    )
    db.session.add(record)

    # Budget warnings are evaluated against the ledger itself. The call is
    # inside the same transaction so a warning notification commits with the
    # usage row; accounting never fails because of budget evaluation.
    from app.services import budgets

    budgets.after_usage_recorded(record)

    if commit:
        db.session.commit()

    # Publish token/cost counters for the protected /metrics endpoint (#370).
    # Recording is best-effort and only touches in-process counters, so it can
    # never change the ledger result or fail an accounting path.
    from app.observability import record_usage as record_metrics_usage

    record_metrics_usage(
        provider or "",
        model or "",
        counts["prompt_tokens"],
        counts["completion_tokens"],
        cost_estimate,
        latency_ms=latency_ms,
    )
    return record


#: Default and hard cap for leaderboard responses.
TOP_DEFAULT_LIMIT = 10
TOP_MAX_LIMIT = 50


def top_entities(*, user_id, limit=TOP_DEFAULT_LIMIT) -> dict:
    """Return bounded top conversations and projects by cost, then tokens.

    ``user_id`` is mandatory so a leaderboard can never cross account
    boundaries. Rows without a conversation/project id are excluded.
    """
    limit = limit if isinstance(limit, int) and limit >= 1 else TOP_DEFAULT_LIMIT
    limit = min(limit, TOP_MAX_LIMIT)

    def _top(column):
        rows = (
            UsageRecord.query.with_entities(
                column.label("id"),
                func.count(UsageRecord.id).label("records"),
                func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(UsageRecord.cost_estimate), 0.0).label("cost_estimate"),
            )
            .filter(UsageRecord.user_id == user_id, column.isnot(None))
            .group_by(column)
            .order_by(
                func.coalesce(func.sum(UsageRecord.cost_estimate), 0.0).desc(),
                func.coalesce(func.sum(UsageRecord.total_tokens), 0).desc(),
                column.asc(),
            )
            .limit(limit)
            .all()
        )
        return [
            {
                "id": int(row.id),
                "records": int(row.records),
                "total_tokens": int(row.total_tokens or 0),
                "cost_estimate": pricing.round_cost(float(row.cost_estimate or 0.0)),
            }
            for row in rows
        ]

    return {
        "limit": limit,
        "conversations": _top(UsageRecord.conversation_id),
        "projects": _top(UsageRecord.project_id),
    }


def summarize_records(records) -> dict:
    """Aggregate counts and costs for an iterable of records."""
    summary = {
        "count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_estimate": 0.0,
        "priced_records": 0,
        "estimated_records": 0,
    }
    for record in records:
        summary["count"] += 1
        summary["prompt_tokens"] += int(getattr(record, "prompt_tokens", 0) or 0)
        summary["completion_tokens"] += int(getattr(record, "completion_tokens", 0) or 0)
        summary["total_tokens"] += int(getattr(record, "total_tokens", 0) or 0)
        if getattr(record, "tokens_estimated", False):
            summary["estimated_records"] += 1
        cost = getattr(record, "cost_estimate", None)
        if cost is not None:
            summary["cost_estimate"] += float(cost)
            summary["priced_records"] += 1
    summary["cost_estimate"] = pricing.round_cost(summary["cost_estimate"])
    return summary


def filtered_records_query(
    *,
    user_id=None,
    workspace_id=None,
    project_id=None,
    source=None,
):
    """Return the base query for rows matching optional owner/scope filters."""
    query = UsageRecord.query
    if user_id is not None:
        query = query.filter(UsageRecord.user_id == user_id)
    if workspace_id is not None:
        query = query.filter(UsageRecord.workspace_id == workspace_id)
    if project_id is not None:
        query = query.filter(UsageRecord.project_id == project_id)
    if source is not None:
        normalized = normalize_source(source)
        if normalized is not None:
            query = query.filter(UsageRecord.source == normalized)
    return query


def query_records(
    *,
    user_id=None,
    workspace_id=None,
    project_id=None,
    source=None,
) -> list[UsageRecord]:
    """Fetch ledger rows with optional owner/scope filters."""
    query = filtered_records_query(
        user_id=user_id,
        workspace_id=workspace_id,
        project_id=project_id,
        source=source,
    )
    return query.order_by(UsageRecord.created_at, UsageRecord.id).all()


def summarize_query(query) -> dict:
    """Aggregate the rows matched by a filtered ledger query.

    Uses SQL aggregates so an export can report totals for the full filtered
    set without loading every row into memory. The returned shape matches
    :func:`summarize_records`.
    """
    count = query.count()
    prompt_tokens = query.with_entities(
        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0)
    ).scalar()
    completion_tokens = query.with_entities(
        func.coalesce(func.sum(UsageRecord.completion_tokens), 0)
    ).scalar()
    total_tokens = query.with_entities(
        func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    ).scalar()
    cost_estimate = query.with_entities(
        func.coalesce(func.sum(UsageRecord.cost_estimate), 0.0)
    ).scalar()
    priced_records = query.filter(UsageRecord.cost_estimate.isnot(None)).count()
    estimated_records = query.filter(UsageRecord.tokens_estimated.is_(True)).count()
    return {
        "count": int(count),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "cost_estimate": pricing.round_cost(float(cost_estimate or 0.0)),
        "priced_records": int(priced_records),
        "estimated_records": int(estimated_records),
    }


def export_payload(
    *,
    user_id,
    workspace_id=None,
    project_id=None,
    source=None,
    page=1,
    limit=EXPORT_DEFAULT_LIMIT,
    exported_at: datetime | None = None,
) -> dict:
    """Build a bounded, paginated JSON export for one user's ledger rows.

    The user scope is mandatory and always applied first, so optional
    workspace/project/source filters can only narrow the caller's own data.
    Invalid page/limit values are clamped to the same bounds as the plugin
    listing (page >= 1, 1 <= limit <= :data:`EXPORT_MAX_LIMIT`) rather than
    producing an unbounded response.

    ``totals`` covers every row matching the filters; ``page_totals`` covers
    only the returned page. Records are serialized with
    :meth:`~app.models.UsageRecord.to_dict`, so field names stay identical to
    the rest of the usage APIs.
    """
    page = page if isinstance(page, int) and page >= 1 else 1
    if not isinstance(limit, int) or limit < 1:
        limit = EXPORT_DEFAULT_LIMIT
    limit = min(limit, EXPORT_MAX_LIMIT)

    normalized_source = normalize_source(source)
    query = filtered_records_query(
        user_id=user_id,
        workspace_id=workspace_id,
        project_id=project_id,
        source=normalized_source,
    )
    totals = summarize_query(query)
    total = totals["count"]
    pages = (total + limit - 1) // limit if total else 0
    offset = (page - 1) * limit
    records = (
        query.order_by(UsageRecord.created_at, UsageRecord.id).offset(offset).limit(limit).all()
    )
    page_totals = summarize_records(records)

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": (exported_at or datetime.now(UTC)).isoformat(),
        "filters": {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "source": normalized_source,
        },
        "pagination": {
            "page": page,
            "limit": limit,
            "max_limit": EXPORT_MAX_LIMIT,
            "total": total,
            "pages": pages,
            "has_next": page < pages,
            "has_prev": page > 1,
        },
        "totals": totals,
        "page_totals": page_totals,
        "records": [record.to_dict() for record in records],
    }


def export_csv_chunks(
    *,
    user_id,
    workspace_id=None,
    project_id=None,
    source=None,
    max_rows: int | None = None,
    currency: str = "USD",
) -> tuple[list[str], bool, int]:
    """Render a bounded CSV export for one user's ledger rows.

    The authenticated user is the mandatory outer scope. Optional
    workspace/project/source filters can only narrow that user's own data.

    Rows are serialized with :mod:`csv` so commas, quotes, and newlines in
    provider/model labels are escaped correctly. At most ``max_rows`` rows are
    emitted (default :data:`EXPORT_CSV_MAX_ROWS`); when the filtered set is
    larger, the caller must advertise truncation rather than implying a complete
    export.

    Returns ``(chunks, truncated, total)``. Chunks are pre-serialized lines so
    the Flask route can stream them without materializing an unbounded body.
    """
    limit = EXPORT_CSV_MAX_ROWS if max_rows is None else max(1, int(max_rows))
    normalized_source = normalize_source(source)
    query = filtered_records_query(
        user_id=user_id,
        workspace_id=workspace_id,
        project_id=project_id,
        source=normalized_source,
    )
    total = query.count()
    records = query.order_by(UsageRecord.created_at, UsageRecord.id).limit(limit).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    chunks: list[str] = []
    writer.writerow(EXPORT_CSV_COLUMNS)
    chunks.append(buffer.getvalue())
    buffer.seek(0)
    buffer.truncate(0)

    for record in records:
        writer.writerow(
            [
                record.created_at.isoformat() if record.created_at else "",
                record.user_id if record.user_id is not None else "",
                record.workspace_id if record.workspace_id is not None else "",
                record.project_id if record.project_id is not None else "",
                record.conversation_id if record.conversation_id is not None else "",
                normalize_source(record.source) or UNKNOWN_SOURCE,
                record.provider or "",
                record.model or "",
                record.status or "",
                int(record.prompt_tokens or 0),
                int(record.completion_tokens or 0),
                int(record.total_tokens or 0),
                bool(record.tokens_estimated),
                "" if record.cost_estimate is None else record.cost_estimate,
                currency,
            ]
        )
        chunks.append(buffer.getvalue())
        buffer.seek(0)
        buffer.truncate(0)

    return chunks, total > len(records), total


def recompute_cost_estimates(
    *,
    user_id=None,
    workspace_id=None,
    batch_size: int = RECOMPUTE_DEFAULT_BATCH_SIZE,
    max_rows: int = RECOMPUTE_DEFAULT_MAX_ROWS,
    dry_run: bool = False,
    recomputed_at: datetime | None = None,
) -> dict:
    """Recompute stored cost estimates from the current price table (#440).

    Only rows whose recomputed value differs are written. Each changed row gets
    ``cost_recomputed_at`` so operators can see when a price change was applied;
    a second run is a no-op (idempotent). Rows whose provider/model has no known
    price are left with ``cost_estimate = None`` rather than being guessed.

    The work is bounded by ``max_rows`` and processed in keyset batches of
    ``batch_size`` so a large ledger is never loaded at once.
    """
    batch_size = max(1, min(int(batch_size), RECOMPUTE_MAX_BATCH_SIZE))
    max_rows = max(0, min(int(max_rows), RECOMPUTE_MAX_ROWS))
    timestamp = recomputed_at or datetime.now(UTC)

    query = filtered_records_query(user_id=user_id, workspace_id=workspace_id)
    cursor = 0
    scanned = updated = unchanged = unpriced = batches = 0

    while scanned < max_rows:
        take = min(batch_size, max_rows - scanned)
        rows = query.filter(UsageRecord.id > cursor).order_by(UsageRecord.id).limit(take).all()
        if not rows:
            break
        batches += 1

        for record in rows:
            scanned += 1
            new_cost = pricing.estimate_cost_usd(
                record.provider,
                record.model,
                prompt_tokens=int(record.prompt_tokens or 0),
                completion_tokens=int(record.completion_tokens or 0),
                workspace_id=record.workspace_id,
            )
            old_cost = record.cost_estimate
            if new_cost is None:
                unpriced += 1
                if old_cost is None:
                    continue
            if new_cost == old_cost:
                unchanged += 1
                continue
            updated += 1
            if not dry_run:
                record.cost_estimate = new_cost
                record.cost_recomputed_at = timestamp

        cursor = rows[-1].id
        if not dry_run:
            db.session.commit()

    return {
        "scanned": scanned,
        "updated": updated,
        "unchanged": unchanged,
        "unpriced": unpriced,
        "batches": batches,
        "dry_run": bool(dry_run),
        "max_rows": max_rows,
        "recomputed_at": None if dry_run else timestamp.isoformat(),
    }


#: Default and maximum number of provider/model groups returned (#450).
BREAKDOWN_DEFAULT_LIMIT = 20
BREAKDOWN_MAX_LIMIT = 100
#: Label used when a historical row has no provider/model.
UNKNOWN_DIMENSION = "unknown"


def _dimension_label(value) -> str:
    text = str(value or "").strip()
    return text[:100] if text else UNKNOWN_DIMENSION


def _merge_summaries(rows: list[dict]) -> dict:
    merged = {
        "count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_estimate": 0.0,
        "priced_records": 0,
        "estimated_records": 0,
    }
    for row in rows:
        for key in merged:
            merged[key] += row.get(key, 0)
    merged["cost_estimate"] = pricing.round_cost(merged["cost_estimate"])
    return merged


def breakdown_by_dimension(
    records,
    attribute: str,
    *,
    limit: int = BREAKDOWN_DEFAULT_LIMIT,
) -> tuple[list[dict], int, bool]:
    """Aggregate records by ``provider`` or ``model`` with a bounded group count.

    Rows with a missing/blank label are grouped under ``unknown`` instead of
    being dropped. Results are sorted by estimated cost, then tokens, then label
    (all descending except the label tie-break). When more than ``limit`` groups
    exist, the remainder is merged into an explicit ``other`` row and
    ``truncated`` is returned as ``True``.
    """
    limit = max(1, min(int(limit), BREAKDOWN_MAX_LIMIT))
    buckets: dict[str, list] = {}
    for record in records:
        label = _dimension_label(getattr(record, attribute, None))
        buckets.setdefault(label, []).append(record)

    rows = [{attribute: label, **summarize_records(items)} for label, items in buckets.items()]
    rows.sort(
        key=lambda row: (
            -row["cost_estimate"],
            -row["total_tokens"],
            row[attribute],
        )
    )
    total_groups = len(rows)
    truncated = total_groups > limit
    if truncated:
        omitted = rows[limit:]
        rows = rows[:limit]
        rows.append({attribute: "other", **_merge_summaries(omitted)})
    return rows, total_groups, truncated


def breakdown_by_source(
    records=None,
    *,
    user_id=None,
    workspace_id=None,
    project_id=None,
) -> dict[str, dict]:
    """Aggregate usage and cost by source.

    All known sources are returned (zeroed when unused) so consumers get a stable
    shape. Rows with a missing or unrecognised ``source`` are grouped under
    ``unknown`` only when such rows exist, which keeps malformed historical data
    visible without breaking the response.
    """
    if records is None:
        records = query_records(user_id=user_id, workspace_id=workspace_id, project_id=project_id)

    buckets: dict[str, list] = {source: [] for source in SOURCES}
    unknown = []
    for record in records:
        bucket = source_bucket(getattr(record, "source", None))
        if bucket == UNKNOWN_SOURCE:
            unknown.append(record)
        else:
            buckets.setdefault(bucket, []).append(record)

    result = {source: summarize_records(buckets[source]) for source in SOURCES}
    if unknown:
        result[UNKNOWN_SOURCE] = summarize_records(unknown)
    return result


def breakdown_payload(
    *,
    user_id=None,
    workspace_id=None,
    project_id=None,
    limit: int = BREAKDOWN_DEFAULT_LIMIT,
) -> dict:
    """Build the JSON-friendly payload used by the breakdown API.

    Source rows keep their stable all-known-sources shape. Provider and model
    rows are bounded lists sorted by cost/tokens, with unknown labels preserved
    as ``unknown`` and overflow merged into ``other``.
    """
    limit = max(1, min(int(limit), BREAKDOWN_MAX_LIMIT))
    records = query_records(user_id=user_id, workspace_id=workspace_id, project_id=project_id)
    by_source = breakdown_by_source(records)
    totals = summarize_records(records)
    by_provider, provider_groups, provider_truncated = breakdown_by_dimension(
        records, "provider", limit=limit
    )
    by_model, model_groups, model_truncated = breakdown_by_dimension(records, "model", limit=limit)
    return {
        "by_source": by_source,
        "sources": [{"source": source, **stats} for source, stats in by_source.items()],
        "by_provider": by_provider,
        "providers": by_provider,
        "by_model": by_model,
        "models": by_model,
        "provider_groups_total": provider_groups,
        "model_groups_total": model_groups,
        "provider_truncated": provider_truncated,
        "model_truncated": model_truncated,
        "group_limit": limit,
        "max_group_limit": BREAKDOWN_MAX_LIMIT,
        "totals": totals,
        "known_sources": list(SOURCES),
    }


def _message_usage(message: Message) -> dict[str, int] | None:
    """Return validated token counts for a message, or ``None`` when absent."""
    values = (
        getattr(message, "prompt_tokens", None),
        getattr(message, "completion_tokens", None),
        getattr(message, "total_tokens", None),
    )
    if all(value is None for value in values):
        return None
    try:
        return normalize_usage(*values)
    except ValueError:
        return None


def _backfill_record_exists(
    message: Message,
    conversation_id: int,
    user_id: int | None,
    counts: dict[str, int],
) -> bool:
    """Return ``True`` when this message was already copied into the ledger.

    A backfilled row carries a stable ``backfill:message:<id>`` sentinel in the
    existing ``request_id`` column, so two messages with identical counts and
    timestamps are still tracked independently. Rows written by older versions
    (before the sentinel existed) are also recognised by the historical tuple
    of source, conversation, counts, and message timestamp.
    """
    if message.id is not None:
        sentinel = f"{BACKFILL_REQUEST_PREFIX}{message.id}"
        if UsageRecord.query.filter_by(source=SOURCE_CHAT, request_id=sentinel).first() is not None:
            return True

    if message.created_at is None:
        return False
    return (
        UsageRecord.query.filter_by(
            source=SOURCE_CHAT,
            conversation_id=conversation_id,
            user_id=user_id,
            prompt_tokens=counts["prompt_tokens"],
            completion_tokens=counts["completion_tokens"],
            total_tokens=counts["total_tokens"],
            request_id=None,
        )
        .filter(UsageRecord.created_at == message.created_at)
        .first()
        is not None
    )


def backfill_from_messages(
    *,
    batch_size: int = BACKFILL_DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Copy historical chat-message token counts into the usage ledger.

    The operation is keyset-paginated and idempotent: existing message rows
    are never modified, and re-running the command only skips rows that were
    already backfilled. ``limit`` bounds the number of messages considered in a
    single invocation, which is useful for a staged rollout. ``dry_run`` reports
    what would be written without committing anything.

    Raises:
        ValueError: If ``batch_size``/``limit`` is outside the supported range.
    """
    if not isinstance(batch_size, int) or not 1 <= batch_size <= BACKFILL_MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be an integer between 1 and {BACKFILL_MAX_BATCH_SIZE}.")
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer.")

    stats = {
        "source": SOURCE_CHAT,
        "scanned": 0,
        "created": 0,
        "skipped_existing": 0,
        "skipped_incomplete": 0,
        "skipped_orphan": 0,
        "batches": 0,
        "batch_size": batch_size,
        "limit": limit,
        "dry_run": bool(dry_run),
    }
    last_id = 0
    processed = 0

    try:
        while limit is None or processed < limit:
            size = batch_size if limit is None else min(batch_size, limit - processed)
            if size <= 0:
                break
            messages = (
                Message.query.filter(Message.id > last_id)
                .filter(
                    or_(
                        Message.prompt_tokens.isnot(None),
                        Message.completion_tokens.isnot(None),
                        Message.total_tokens.isnot(None),
                    )
                )
                .order_by(Message.id)
                .limit(size)
                .all()
            )
            if not messages:
                break

            stats["batches"] += 1
            for message in messages:
                if limit is not None and processed >= limit:
                    break
                last_id = message.id
                processed += 1
                stats["scanned"] += 1

                counts = _message_usage(message)
                if counts is None:
                    stats["skipped_incomplete"] += 1
                    continue
                conversation = message.conversation
                if conversation is None:
                    stats["skipped_orphan"] += 1
                    continue
                if _backfill_record_exists(
                    message,
                    message.conversation_id,
                    conversation.user_id,
                    counts,
                ):
                    stats["skipped_existing"] += 1
                    continue
                if dry_run:
                    stats["created"] += 1
                    continue

                record = record_usage(
                    source=SOURCE_CHAT,
                    prompt_tokens=counts["prompt_tokens"],
                    completion_tokens=counts["completion_tokens"],
                    total_tokens=counts["total_tokens"],
                    # Historical columns do not record whether the provider
                    # reported the counts or they were estimated.
                    tokens_estimated=True,
                    user_id=conversation.user_id,
                    conversation_id=message.conversation_id,
                    request_id=f"{BACKFILL_REQUEST_PREFIX}{message.id}",
                    commit=False,
                )
                record.created_at = message.created_at
                stats["created"] += 1

            if not dry_run:
                db.session.commit()
            if len(messages) < size:
                break
    except Exception:
        db.session.rollback()
        raise

    return stats


def retention_days_from_config(config: Any | None = None) -> int:
    """Return the configured usage retention window in days.

    Reads ``USAGE_RETENTION_DAYS`` from ``config`` (a Flask config or mapping).
    Falls back to the current app config when available, then to
    :data:`DEFAULT_RETENTION_DAYS`. Values below :data:`MIN_RETENTION_DAYS` are
    clamped so a purge can never target rows from the current day.
    """
    source = config
    if source is None:
        from flask import current_app, has_app_context

        if has_app_context():
            source = current_app.config
    value = None
    if source is not None:
        try:
            value = source.get("USAGE_RETENTION_DAYS")
        except AttributeError:
            value = None
    try:
        days = int(value) if value is not None else DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return max(days, MIN_RETENTION_DAYS)


def purge_records(
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    commit: bool = True,
) -> dict:
    """Delete usage rows older than the retention window.

    A row is purged only when its ``created_at`` is **strictly older** than
    ``cutoff`` (``now - retention_days``). Rows at or newer than the cutoff are
    always retained, so a boundary row is never removed. ``dry_run`` reports the
    count without deleting anything.

    Returns a stats dict describing the window and the number removed.
    """
    if retention_days is None:
        retention_days = retention_days_from_config()
    if not isinstance(retention_days, int) or retention_days < MIN_RETENTION_DAYS:
        raise ValueError(f"retention_days must be an integer >= {MIN_RETENTION_DAYS}")

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    cutoff = moment - timedelta(days=retention_days)

    stale_query = UsageRecord.query.filter(UsageRecord.created_at < cutoff)
    deleted = stale_query.count() if dry_run else stale_query.delete(synchronize_session=False)
    if commit and not dry_run:
        db.session.commit()

    return {
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
        "deleted": int(deleted),
        "dry_run": bool(dry_run),
    }


__all__ = [
    "BACKFILL_DEFAULT_BATCH_SIZE",
    "BACKFILL_MAX_BATCH_SIZE",
    "BACKFILL_REQUEST_PREFIX",
    "DEFAULT_RETENTION_DAYS",
    "EXPORT_DEFAULT_LIMIT",
    "EXPORT_MAX_LIMIT",
    "EXPORT_SCHEMA_VERSION",
    "MIN_RETENTION_DAYS",
    "SOURCES",
    "SOURCE_ANALYSIS",
    "SOURCE_CHAT",
    "SOURCE_PROJECT_CHAT",
    "SOURCE_REVIEW",
    "SOURCE_TOOLS",
    "STATUS_DENIED",
    "STATUS_ERROR",
    "STATUS_SUCCESS",
    "TOP_DEFAULT_LIMIT",
    "TOP_MAX_LIMIT",
    "UNKNOWN_SOURCE",
    "backfill_from_messages",
    "breakdown_by_source",
    "breakdown_payload",
    "current_provider_metadata",
    "estimate_cost",
    "export_payload",
    "filtered_records_query",
    "normalize_source",
    "normalize_usage",
    "purge_records",
    "query_records",
    "record_usage",
    "retention_days_from_config",
    "source_bucket",
    "summarize_query",
    "summarize_records",
    "top_entities",
]
