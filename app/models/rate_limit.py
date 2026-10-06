"""Persistent rate-limit counters.

A single row per bucket, so per-user limits survive a process restart and are
shared across workers (the in-memory sliding window in
:mod:`app.services.ratelimit` cannot do either). The daily chat cap uses one row
per user and UTC day; the date is part of the bucket ``key`` so a new day
naturally starts from zero without any cleanup job.
"""

from datetime import UTC, datetime

from app.extensions import db


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RateLimit(db.Model):
    """A persisted counter for a single rate-limit bucket."""

    __tablename__ = "rate_limits"

    id = db.Column(db.Integer, primary_key=True)
    #: Fully-qualified bucket key (for the daily cap this includes the UTC date).
    key = db.Column(db.String(255), nullable=False, unique=True, index=True)
    #: Start of the window the counter belongs to.
    window_start = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    #: Requests recorded against this bucket within its window.
    hits = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RateLimit key={self.key!r} hits={self.hits}>"
