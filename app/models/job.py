"""Persisted background job for the bounded worker queue (#699).

A job is a durable row so work survives a process restart. The worker claims
due rows, runs the registered handler, and either marks the job succeeded or
requeues it with exponential backoff until ``max_attempts`` is exhausted.

Payloads are JSON text. They can contain identifiers the handler needs, so the
model's serializer omits the payload from list/CLI output by default.
"""

from datetime import UTC, datetime

from app.extensions import db

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)


class Job(db.Model):
    """One durable unit of background work."""

    __tablename__ = "jobs"
    __table_args__ = (
        # Worker claim: due queued jobs ordered by availability.
        db.Index("ix_jobs_status_available", "status", "available_at"),
        # Operator views/filtering by kind and outcome.
        db.Index("ix_jobs_kind_status", "kind", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(50), nullable=False)
    payload = db.Column(db.Text, nullable=False, default="{}")
    status = db.Column(db.String(20), nullable=False, default=STATUS_QUEUED)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=5)
    available_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    last_error = db.Column(db.String(2000), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def to_dict(self, *, include_payload: bool = False) -> dict:
        """Serialize the job for CLI/API output without leaking its payload."""
        data = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_payload:
            data["payload"] = self.payload
        return data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job id={self.id} kind={self.kind!r} status={self.status!r}>"
