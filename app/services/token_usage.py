"""Token usage accounting for chat messages and conversations (issue #13).

Provider responses carry real prompt/completion token counts when the vendor
reports them; when they do not — the offline mock provider, or a streamed reply
— a small, dependency-free estimate is used so every message still records a
usage figure.

This module also exposes the daily aggregation and cost estimation helpers
used by the /usage dashboard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app import db
from app.models import TokenUsage

#: Rough characters-per-token ratio for source code and English prose.
CHARS_PER_TOKEN = 4

#: Estimated USD cost per 1K tokens, split by prompt and completion.
#: These are conservative defaults for the bundled mock/open providers.
PRICE_PER_MI_PROMPT = 0.0015
PRICE_PER_MI_COMPLETION = 0.002


def estimate_tokens(text) -> int:
    """Estimate the token count of `text` (>= 1 for non-empty input)."""
    if text is None:
        return 0
    value = str(text)
    if not value:
        return 0
    return max(1, (len(value) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def messages_text(messages) -> str:
    """Join the content of a provider message list into one string."""
    parts = []
    for message in messages or []:
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        if content:
            parts.append(str(content))
    return "\n".join(parts)


def _totals(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def usage_from_response(response, messages=None) -> dict:
    """Usage for a completed provider response, estimating any missing counts."""
    prompt = getattr(response, "prompt_tokens", None)
    completion = getattr(response, "completion_tokens", None)
    if prompt is None:
        prompt = estimate_tokens(messages_text(messages))
    if completion is None:
        completion = estimate_tokens(getattr(response, "content", ""))
    total = getattr(response, "total_tokens", None)
    if total is None:
        total = (prompt or 0) + (completion or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def usage_from_text(prompt_text, completion_text) -> dict:
    """Usage estimated from raw prompt/completion text (e.g. a streamed reply)."""
    return _totals(estimate_tokens(prompt_text), estimate_tokens(completion_text))


def sum_usage(messages) -> dict:
    """Cumulative usage across `messages` (missing counts ignored)."""
    prompt = completion = total = 0
    for message in messages or []:
        if getattr(message, "total_tokens", None) is None:
            continue
        prompt += message.prompt_tokens or 0
        completion += message.completion_tokens or 0
        total += message.total_tokens or 0
    return _totals(prompt, completion)


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate the USD cost of a token bucket."""
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cost = (prompt / 1000.0) * PRICE_PER_MI_PROMPT
    cost += (completion / 1000.0) * PRICE_PER_MIN_COMPLETION
    return round(cost, 6)


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _range_start(days: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=max(1, int(days)) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start


def _query(user_id, start: datetime, end: datetime | None = None):
    query = TokenUsage.query.filter(TokenUsage.user_id == user_id)
    query = query.filter(TokenUsage.created_at >= start)
    if end is not None:
        query = query.filter(TokenUsage.created_at < end)
    return query


def monthly_summary(user_id, now: datetime | None = None) -> dict:
    """Total tokens and estimated cost for the current calendar month."""
    now = now or datetime.now(timezone.utc)
    start = _month_start(now)
    rows = _query(user_id, start).all()
    prompt = sum(int(row.prompt_tokens or 0) for row in rows)
    completion = sum(int(row.completion_tokens or 0) for row in rows)
    total = sum(int(row.total_tokens or 0) for row in rows)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated_cost_usd": estimate_cost(prompt, completion),
        "message_count": len(rows),
        "period_start": start.isoformat(),
        "period_end": now.isoformat(),
    }


def daily_usage(user_id, days: int = 30, now: datetime | None = None) -> list[dict]:
    """Per-day token totals for the last `days` days, including empty days."""
    now = now or datetime.now(timezone.utc)
    days = max(1, int(days))
    start = _range_start(days, now)
    buckets: dict[date, dict] = {}
    for offset in range(days):
        day = (start + timedelta(days=offset)).date()
        buckets[day] = {
            "date": day.isoformat(),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
    for row in _query(user_id, start).order_by(TokenUsage.created_at.asc()).all():
        created = row.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        day = created.astimezone(timezone.utc).date()
        bucket = buckets.get(day)
        if bucket is None:
            continue
        bucket["prompt_tokens"] += int(row.prompt_tokens or 0)
        bucket["completion_tokens"] += int(row.completion_tokens or 0)
        bucket["total_tokens"] += int(row.total_tokens or 0)
    for bucket in buckets.values():
        bucket["estimated_cost_usd"] = estimate_cost(
            bucket["prompt_tokens"], bucket["completion_tokens"]
        )
    return [list(buckets.values())[i] for i in sorted(buckets)]


def usage_summary(user_id, days: int = 30, now: datetime | None = None) -> dict:
    """Combined monthly totals and daily series for the dashboard."""
    now = now or datetime.now(timezone.utc)
    return {
        "month": monthly_summary(user_id, now),
        "daily": daily_usage(user_id, days, now),
    }


def record_usage(user_id, model, usage, conversation_id=None, message_id=None) -> TokenUsage:
    """Persist a token usage row for a single message."""
    usage = usage or {}
    row = TokenUsage(
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        model=model,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
    )
    db.session.add(row)
    db.session.commit()
    return row
