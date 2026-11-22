"""Persisted idempotency keys for retry-safe POST requests (#649).

A key is scoped to the caller (``owner``), the endpoint rule, and the client
supplied ``Idempotency-Key``. The row stores a hash of the canonical request
body and the response that was produced the first time, so a retry can replay
that response without repeating side effects.

Rows are intentionally short lived. ``expires_at`` is indexed and expired rows
are removed whenever the service is used (and by ``flask idempotency purge``).
The request body itself is never stored, only its SHA-256 digest.
"""

from datetime import UTC, datetime

from app.extensions import db


class IdempotencyKey(db.Model):
    """One request/response pair retained for a bounded TTL."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        db.UniqueConstraint(
            "owner",
            "endpoint",
            "key",
            name="uq_idempotency_keys_owner_endpoint_key",
        ),
        db.Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    #: The authenticated user, or ``NULL`` for anonymous callers. ``owner``
    #: carries the actual scope because SQL unique constraints treat NULLs as
    #: distinct.
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner = db.Column(db.String(80), nullable=False, index=True)
    endpoint = db.Column(db.String(255), nullable=False)
    key = db.Column(db.String(255), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    #: ``NULL`` while the first request is still in flight. A concurrent retry
    #: sees this reservation and receives a structured conflict instead of
    #: running the side effect twice.
    status_code = db.Column(db.Integer, nullable=True)
    response_body = db.Column(db.Text, nullable=True)
    #: JSON object of response headers safe to replay.
    response_headers = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)

    @property
    def response_status(self) -> int | None:
        """Alias used by callers that describe the stored response status."""
        return self.status_code

    @property
    def request_body_hash(self) -> str:
        """Alias for the stored request digest."""
        return self.request_hash

    @property
    def idempotency_key(self) -> str:
        """Alias for the client supplied key value."""
        return self.key

    @property
    def is_complete(self) -> bool:
        return self.status_code is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IdempotencyKey owner={self.owner!r} endpoint={self.endpoint!r}>"
