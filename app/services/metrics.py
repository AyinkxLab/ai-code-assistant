"""Quality dashboard metrics.

Every metric is computed strictly from persisted ``Review`` and
``ReviewFinding`` rows — never invented, extrapolated, or estimated. When there
are no reviews yet the metrics simply reflect that state.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from app.models import Review, ReviewFinding

SEVERITY_ORDER = ("critical", "high", "medium", "low", "informational")
HIGH_RISK_SEVERITIES = ("critical", "high")


def _recent(count: int) -> datetime:
    # SQLite returns naive datetimes even for timezone-aware columns, so
    # compare against naive UTC to keep both database backends consistent.
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=count)


def _finding_metrics(findings: list) -> dict:
    high_risk = [f for f in findings if f.severity in HIGH_RISK_SEVERITIES]
    unaddressed = [f for f in high_risk if not f.addressed]
    return {
        "total": len(findings),
        "by_severity": dict.fromkeys(SEVERITY_ORDER, 0)
        | dict(Counter(f.severity for f in findings)),
        "by_category": dict(Counter(f.category for f in findings).most_common()),
        "by_confidence": dict(Counter(f.confidence for f in findings)),
        "by_file": dict(Counter(f.file or "(unknown)" for f in findings).most_common(10)),
        "high_risk": len(high_risk),
        "unaddressed_high_risk": len(unaddressed),
        "addressed": sum(1 for f in findings if f.addressed),
        "confirmed": sum(1 for f in findings if f.confidence == "confirmed"),
        "potential": sum(1 for f in findings if f.confidence == "potential"),
        "suggestion": sum(1 for f in findings if f.confidence == "suggestion"),
    }


def _findings_trend(reviews: list, findings: list) -> list[dict]:
    """Per-review open vs addressed finding counts, oldest first.

    Each point is ``{"review_id", "created_at", "kind", "open", "addressed",
    "total"}`` so the quality dashboard can show how findings are being
    addressed over time. Counts come only from persisted ``ReviewFinding`` rows;
    nothing is inferred.
    """
    counts = {r.id: {"open": 0, "addressed": 0} for r in reviews}
    for finding in findings:
        bucket = counts.get(finding.review_id)
        if bucket is None:
            continue
        bucket["addressed" if finding.addressed else "open"] += 1
    ordered = sorted(reviews, key=lambda r: (r.created_at or datetime.min, r.id))
    trend = []
    for review in ordered:
        bucket = counts[review.id]
        trend.append(
            {
                "review_id": review.id,
                "created_at": review.created_at.isoformat() if review.created_at else None,
                "kind": review.kind,
                "open": bucket["open"],
                "addressed": bucket["addressed"],
                "total": bucket["open"] + bucket["addressed"],
            }
        )
    return trend


def review_metrics(reviews: list, findings: list) -> dict:
    """Compute dashboard metrics from already-fetched reviews and findings."""
    seven_days = _recent(7)
    last = max((r.created_at for r in reviews), default=None)
    metrics = {
        "total_reviews": len(reviews),
        "by_status": dict(Counter(r.status for r in reviews)),
        "by_kind": dict(Counter(r.kind for r in reviews)),
        "by_source": dict(Counter(r.source for r in reviews)),
        "findings": _finding_metrics(findings),
        "last_review_at": last.isoformat() if last else None,
        "reviews_last_7_days": sum(
            1 for r in reviews if r.created_at and r.created_at >= seven_days
        ),
        "reviews_last_30_days": sum(
            1 for r in reviews if r.created_at and r.created_at >= _recent(30)
        ),
    }
    kind_by_review = {r.id: r.kind for r in reviews}
    by_kind = {}
    for finding in findings:
        kind = kind_by_review.get(finding.review_id, "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    metrics["findings"]["by_kind"] = by_kind
    metrics["findings_trend"] = _findings_trend(reviews, findings)
    return metrics


def _findings_for(review_ids: list[int]) -> list:
    """Fetch all findings belonging to ``review_ids``."""
    if not review_ids:
        return []
    return ReviewFinding.query.filter(ReviewFinding.review_id.in_(review_ids)).all()


def project_metrics(project) -> dict:
    """Metrics for one project's reviews (owner-scoped by the caller)."""
    reviews = Review.query.filter_by(project_id=project.id).order_by(Review.created_at).all()
    return review_metrics(reviews, _findings_for([r.id for r in reviews]))


def workspace_metrics(workspace) -> dict:
    """Aggregated metrics across every project in a workspace."""
    reviews = (
        Review.query.join(Review.project)
        .filter_by(workspace_id=workspace.id)
        .order_by(Review.created_at)
        .all()
    )
    return review_metrics(reviews, _findings_for([r.id for r in reviews]))


def user_metrics(user) -> dict:
    """Aggregated metrics across every review the user owns."""
    reviews = Review.query.filter_by(user_id=user.id).order_by(Review.created_at).all()
    return review_metrics(reviews, _findings_for([r.id for r in reviews]))
