"""Weekly usage digest aggregation (issue #474).

Totals come from the append-only usage ledger. Rules, from docs/billing.md:
- denied rows never reached a provider, so they are excluded;
- unpriced rows (cost_estimate NULL) are counted separately, never as $0.00.

This module only aggregates. Opt-out and delivery live in the caller.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_

from app.extensions import db
from app.models.usage_record import UsageRecord

DIGEST_WINDOW = timedelta(days=7)
MAX_SOURCE_ROWS = 10
SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"


@dataclass(frozen=True)
class DigestTotals:
    calls: int
    total_tokens: int
    #: Sum of priced rows only. Unpriced rows contribute nothing here and are
    #: reported in ``unpriced_calls``.
    cost_estimate: float
    unpriced_calls: int


@dataclass(frozen=True)
class DigestSummary:
    scope: str
    scope_id: int
    window_start: datetime
    window_end: datetime
    totals: DigestTotals
    #: (source, totals) pairs, largest token count first, bounded by MAX_SOURCE_ROWS.
    by_source: list[tuple[str, DigestTotals]]
    sources_truncated: bool


def _scope_filter(scope: str, scope_id: int):
    if scope == SCOPE_USER:
        return UsageRecord.user_id == scope_id
    if scope == SCOPE_WORKSPACE:
        return UsageRecord.workspace_id == scope_id
    raise ValueError(f"unknown digest scope: {scope!r}")


def _window_filter(start: datetime, end: datetime):
    return (
        UsageRecord.created_at >= start,
        UsageRecord.created_at < end,
        # status is nullable. A plain status != 'denied' would drop NULL rows,
        # so NULL is kept explicitly.
        or_(UsageRecord.status.is_(None), UsageRecord.status != "denied"),
    )


def _aggregates():
    return (
        func.count(UsageRecord.id),
        func.coalesce(func.sum(UsageRecord.total_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost_estimate), 0.0),
        func.count(UsageRecord.id).filter(UsageRecord.cost_estimate.is_(None)),
    )


def _totals(row) -> DigestTotals:
    calls, tokens, cost, unpriced = row
    return DigestTotals(
        calls=int(calls),
        total_tokens=int(tokens),
        cost_estimate=float(cost),
        unpriced_calls=int(unpriced),
    )


def build_digest(scope: str, scope_id: int, *, end: datetime | None = None) -> DigestSummary:
    """Summarize one scope's usage over the seven days ending at ``end``.

    The window is half-open, ``[start, end)``, so a record exactly at ``end``
    belongs to the next digest and is counted once.
    """
    window_end = end or datetime.now(UTC)
    window_start = window_end - DIGEST_WINDOW
    filters = (_scope_filter(scope, scope_id), *_window_filter(window_start, window_end))

    totals_row = db.session.query(*_aggregates()).filter(*filters).one()

    source_rows = (
        db.session.query(UsageRecord.source, *_aggregates())
        .filter(*filters)
        .group_by(UsageRecord.source)
        .order_by(func.sum(UsageRecord.total_tokens).desc(), UsageRecord.source)
        .limit(MAX_SOURCE_ROWS + 1)
        .all()
    )

    return DigestSummary(
        scope=scope,
        scope_id=scope_id,
        window_start=window_start,
        window_end=window_end,
        totals=_totals(totals_row),
        by_source=[(row[0], _totals(row[1:])) for row in source_rows[:MAX_SOURCE_ROWS]],
        sources_truncated=len(source_rows) > MAX_SOURCE_ROWS,
    )
