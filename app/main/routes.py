"""Public and dashboard routes."""

from datetime import datetime, timedelta

from flask import abort, jsonify, render_template
from flask_login import current_user, login_required

from app.main import bp


@bp.route("/")
def index():
    """Landing page. Prompts the user to sign in or register."""
    return render_template("main/index.html")


@bp.route("/health")
def health():
    """Lightweight health-check endpoint used by Docker and CI."""
    return jsonify({"status": "ok", "service": "ai-code-assistant"})


def _usage_query():
    """Return the TokenUsage query object scoped to the current user."""
    from app.models import TokenUsage

    return TokenUsage.query.filter(TokenUsage.user_id == current_user.id)


def _usage_summary():
    """Compute token totals and estimated cost for the current month."""
    from sqlalchemy import func

    from app.models import TokenUsage

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    totals = (
        _usage_query()
        .filter(TokenUsage.created_at >= month_start)
        .with_entities(
            func.coalesce(func.sum(TokenUsage.prompt_tokens), 0),
            func.coalesce(func.sum(TokenUsage.completion_tokens), 0),
            func.coalesce(func.sum(TokenUsage.cost), 0.0),
        )
        .one()
    )

    prompt_totals, completion_totals, cost_total = totals

    return {
        "prompt_tokens": int(prompt_totals or 0),
        "completion_tokens": int(completion_totals or 0),
        "total_tokens": int((prompt_totals or 0) + (completion_totals or 0)),
        "estimated_cost": round(float(cost_total or 0.0), 6),
    }


def _daily_usage(days=30):
    """Return per-day token totals for the last `days` days."""
    from sqlalchemy import func

    from app.models import TokenUsage

    now = datetime.utcnow()
    start = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    rows = (
        _usage_query()
        .filter(TokenUsage.created_at >= start)
        .with_entities(
            func.date(TokenUsage.created_at).label("day"),
            func.coalesce(func.sum(TokenUsage.prompt_tokens), 0),
            func.coalesce(func.sum(TokenUsage.completion_tokens), 0),
            func.coalesce(func.sum(TokenUsage.cost), 0.0),
        )
        .group_by("day")
        .order_by("day")
        .all()
    )

    by_day = {}
    for day, prompt_tokens, completion_tokens, cost in rows:
        by_day[day.isoformat()] = {
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int((prompt_tokens or 0) + (completion_tokens or 0)),
            "estimated_cost": round(float(cost or 0.0), 6),
        }

    series = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        entry = by_day.get(day, {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost": 0.0,
        })
        entry["day"] = day
        series.append(entry)

    return series


@bp.route("/usage")
@login_required
def usage():
    """Render the usage and cost dashboard for the current user."""
    summary = _usage_summary()
    daily = _daily_usage()
    return render_template(
        "main/usage.html",
        summary=summary,
        daily=daily,
    )


@bp.route("/usage/summary")
@lsugage_required
def usage_summary():
    """JSON endpoint returning the current user's usage summary and daily series."""
    return jsonify(
        {
            "summary": _usage_summary(),
            "daily": _daily_usage(),
        }
    )
