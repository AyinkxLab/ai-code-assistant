"""Monthly invoice generation from the usage ledger (Phase 10, issue #482).

Generates a **draft** invoice by aggregating :class:`~app.models.UsageRecord`
rows for a calendar-month billing period. Invoices are always marked as drafts
because cost figures are estimates computed from the internal price table, not
provider-confirmed charges.

Scope options
-------------
- ``user`` - all ledger rows for one user (``scope_id`` = user id).
- ``workspace`` - all ledger rows for one workspace (``scope_id`` = workspace id).

Line items
----------
Each unique ``(provider, model)`` pair in the period becomes one line item.
Unknown provider or model labels are preserved as ``"unknown"`` so historical
rows with missing metadata are still visible rather than silently dropped.

Usage::

    from app.services.invoices import generate_invoice

    invoice = generate_invoice(scope="user", scope_id=1, period="2026-10")
    # Returns a dict with keys: scope, scope_id, period, draft, line_items, totals
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import extract

from app.models import UsageRecord
from app.services import pricing
from app.services.billing_reconciliation import PERIOD_RE

#: Label applied to a provider/model column that is NULL or blank.
UNKNOWN_LABEL = "unknown"

#: Supported scope values.
SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"
_VALID_SCOPES = {SCOPE_USER, SCOPE_WORKSPACE}


class InvoiceError(ValueError):
    """Raised when invoice parameters are invalid."""

    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


def _validate_period(period: Any) -> str:
    """Return a validated ``YYYY-MM`` period string.

    Raises :class:`InvoiceError` when the value does not match ``YYYY-MM``.
    ``YYYY-MM-DD`` input is accepted and truncated to the month.
    """
    text = str(period or "").strip()
    if len(text) >= 7:
        text = text[:7]
    if not PERIOD_RE.match(text):
        raise InvoiceError(
            f"Invalid billing period {period!r}; expected YYYY-MM.",
            kind="period",
        )
    return text


def _label(value: Any) -> str:
    """Return a non-blank string label, falling back to ``UNKNOWN_LABEL``."""
    if value is None:
        return UNKNOWN_LABEL
    text = str(value).strip()
    return text or UNKNOWN_LABEL


def _period_filter(query, period: str):
    """Apply a calendar-month filter for ``period`` (``YYYY-MM``) to a query."""
    year_str, month_str = period.split("-")
    year = int(year_str)
    month = int(month_str)
    return query.filter(
        extract("year", UsageRecord.created_at) == year,
        extract("month", UsageRecord.created_at) == month,
    )


def _scope_filter(query, scope: str, scope_id: int):
    """Apply a scope (user or workspace) filter to a query."""
    if scope == SCOPE_USER:
        return query.filter(UsageRecord.user_id == scope_id)
    return query.filter(UsageRecord.workspace_id == scope_id)


def _build_line_items(records: list[UsageRecord]) -> list[dict]:
    """Aggregate records by ``(provider, model)`` into sorted line items.

    Each line item contains:

    - ``provider`` / ``model`` - dimension labels (``"unknown"`` for NULL/blank).
    - ``record_count`` - number of ledger rows contributing to the line.
    - ``prompt_tokens``, ``completion_tokens``, ``total_tokens`` - summed token counts.
    - ``cost_estimate`` - summed cost estimate, rounded to six decimal places.
      ``None`` when every contributing record has no known price.
    - ``cost_is_estimate`` - always ``True``; callers must not present these
      figures as provider-confirmed charges.
    """
    buckets: dict[tuple[str, str], dict] = {}

    for record in records:
        key = (_label(record.provider), _label(record.model))
        if key not in buckets:
            buckets[key] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "_cost_sum": 0.0,
                "_has_cost": False,
                "record_count": 0,
            }
        bucket = buckets[key]
        bucket["record_count"] += 1
        bucket["prompt_tokens"] += int(record.prompt_tokens or 0)
        bucket["completion_tokens"] += int(record.completion_tokens or 0)
        bucket["total_tokens"] += int(record.total_tokens or 0)
        if record.cost_estimate is not None:
            bucket["_cost_sum"] += float(record.cost_estimate)
            bucket["_has_cost"] = True

    line_items: list[dict] = []
    for (provider, model), bucket in sorted(buckets.items()):
        cost = pricing.round_cost(bucket["_cost_sum"]) if bucket["_has_cost"] else None
        line_items.append(
            {
                "provider": provider,
                "model": model,
                "record_count": bucket["record_count"],
                "prompt_tokens": bucket["prompt_tokens"],
                "completion_tokens": bucket["completion_tokens"],
                "total_tokens": bucket["total_tokens"],
                "cost_estimate": cost,
                "cost_is_estimate": True,
            }
        )

    return line_items


def _totals(line_items: list[dict]) -> dict:
    """Sum line-item fields into an invoice total.

    ``cost_estimate`` is ``None`` when every line item has no known price.
    ``cost_is_estimate`` is always ``True``.
    """
    total_records = 0
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    cost_sum = 0.0
    has_cost = False

    for item in line_items:
        total_records += item["record_count"]
        total_prompt += item["prompt_tokens"]
        total_completion += item["completion_tokens"]
        total_tokens += item["total_tokens"]
        if item["cost_estimate"] is not None:
            cost_sum += float(item["cost_estimate"])
            has_cost = True

    return {
        "record_count": total_records,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "cost_estimate": pricing.round_cost(cost_sum) if has_cost else None,
        "cost_is_estimate": True,
    }


def generate_invoice(
    *,
    scope: str,
    scope_id: int,
    period: str,
    generated_at: datetime | None = None,
) -> dict:
    """Generate a draft monthly invoice for ``scope_id`` in ``period``.

    Parameters
    ----------
    scope:
        ``"user"`` or ``"workspace"``.
    scope_id:
        The user id or workspace id to aggregate ledger rows for.
    period:
        A ``YYYY-MM`` calendar month string.
    generated_at:
        Timestamp for the ``generated_at`` field. Defaults to now (UTC).

    Returns
    -------
    dict
        A JSON-serialisable invoice document with keys:

        - ``scope`` - ``"user"`` or ``"workspace"``.
        - ``scope_id`` - the integer id used.
        - ``period`` - the validated ``YYYY-MM`` string.
        - ``draft`` - always ``True``.
        - ``generated_at`` - ISO-8601 timestamp.
        - ``line_items`` - list of ``(provider, model)`` aggregates.
        - ``totals`` - sum across all line items.

    Raises
    ------
    InvoiceError
        When ``scope`` or ``period`` is invalid.
    """
    scope = str(scope or "").strip().lower()
    if scope not in _VALID_SCOPES:
        raise InvoiceError(
            f"scope must be one of {sorted(_VALID_SCOPES)!r}; got {scope!r}.",
            kind="scope",
        )

    try:
        scope_id = int(scope_id)
    except (TypeError, ValueError) as exc:
        raise InvoiceError("scope_id must be an integer.", kind="invalid") from exc

    period = _validate_period(period)
    timestamp = generated_at or datetime.now(UTC)

    query = UsageRecord.query
    query = _scope_filter(query, scope, scope_id)
    query = _period_filter(query, period)
    records: list[UsageRecord] = query.order_by(UsageRecord.created_at, UsageRecord.id).all()

    line_items = _build_line_items(records)

    return {
        "scope": scope,
        "scope_id": scope_id,
        "period": period,
        "draft": True,
        "generated_at": timestamp.isoformat(),
        "line_items": line_items,
        "totals": _totals(line_items),
    }


__all__ = [
    "SCOPE_USER",
    "SCOPE_WORKSPACE",
    "UNKNOWN_LABEL",
    "InvoiceError",
    "generate_invoice",
]
