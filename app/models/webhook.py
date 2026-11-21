"""Webhook subscriptions and delivery logs (Phase 12, #670 and #672).

A subscription belongs to a workspace and targets a public HTTP(S) endpoint. A
delivery row records one attempt: its outcome, HTTP response code, latency, and
a short error label. Delivery rows deliberately never store the response body
or the subscription secret, so the delivery-log UI cannot leak them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime

from app.extensions import db

DELIVERY_SUCCESS = "success"
DELIVERY_FAILURE = "failure"
DELIVERY_STATUSES = (DELIVERY_SUCCESS, DELIVERY_FAILURE)

_EVENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


def hash_webhook_secret(secret: str) -> str:
    """Return the SHA-256 hex digest of a signing secret."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("webhook secret must be a non-empty string")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_event_types(values: Iterable[str] | str) -> list[str]:
    """Validate, lowercase, de-duplicate, and sort event names."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        raise ValueError("event types must be a string or an iterable of strings")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("event type must be a string")
        event = value.strip().lower()
        if not event:
            raise ValueError("event type must not be empty")
        if not _EVENT_PATTERN.match(event):
            raise ValueError(f"invalid event type: {value!r}")
        normalized.add(event)
    if not normalized:
        raise ValueError("at least one event type is required")
    return sorted(normalized)


class WebhookSubscription(db.Model):
    """A workspace's subscription to deliver events to a URL."""

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        db.Index(
            "ix_webhook_subscriptions_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url = db.Column(db.String(2000), nullable=False)
    event_type = db.Column(db.String(100), nullable=False, default="*")
    # Signing secret. Never serialized in any response; delivery logs omit it.
    secret = db.Column(db.String(255), nullable=True)
    # SHA-256 of the signing secret. Lets the application detect rotation
    # without exposing or decrypting the secret itself.
    secret_hash = db.Column(db.String(64), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    deliveries = db.relationship(
        "WebhookDelivery",
        back_populates="subscription",
        cascade="all, delete-orphan",
        order_by="WebhookDelivery.created_at",
    )
    event_filters = db.relationship(
        "WebhookSubscriptionEvent",
        back_populates="subscription",
        cascade="all, delete-orphan",
        order_by="WebhookSubscriptionEvent.event_type",
    )

    def set_secret(self, secret: str) -> None:
        """Store the signing secret and its hash."""
        self.secret_hash = hash_webhook_secret(secret)
        self.secret = secret

    def signing_secret(self) -> str | None:
        """Return the plaintext signing secret, when configured."""
        return self.secret

    def event_types(self) -> list[str]:
        """The subscribed event names.

        Falls back to the legacy single ``event_type`` column when no explicit
        filters exist so existing subscriptions keep working.
        """
        if self.event_filters:
            return [event.event_type for event in self.event_filters]
        return [self.event_type] if self.event_type else []

    def matches(self, event_type: str) -> bool:
        """Whether this active subscription should receive ``event_type``."""
        if not self.is_active:
            return False
        types = self.event_types()
        return "*" in types or event_type in types

    def set_events(self, values: Iterable[str] | str) -> None:
        """Replace the event filters after validation."""
        self.event_filters = [
            WebhookSubscriptionEvent(event_type=event) for event in normalize_event_types(values)
        ]

    def to_dict(self) -> dict:
        """Serialize the subscription without its secret or full URL."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "url": self.url,
            "event_type": self.event_type,
            "events": self.event_types(),
            "is_active": bool(self.is_active),
            "has_secret": bool(self.secret or self.secret_hash),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WebhookSubscription id={self.id} workspace_id={self.workspace_id}>"


class WebhookDelivery(db.Model):
    """One delivery attempt for a subscription."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        db.Index(
            "ix_webhook_deliveries_subscription_created",
            "subscription_id",
            "created_at",
        ),
        db.Index(
            "ix_webhook_deliveries_subscription_status_created",
            "subscription_id",
            "status",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(100), nullable=False)
    attempt = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(20), nullable=False, index=True)
    response_code = db.Column(db.Integer, nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    # Short error label only (for example ``HTTP 503``); never a response body.
    error = db.Column(db.String(500), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    subscription = db.relationship("WebhookSubscription", back_populates="deliveries")

    def to_dict(self) -> dict:
        """Serialize one attempt for the delivery-log API.

        No secret, request body, or response body is ever included.
        """
        return {
            "id": self.id,
            "subscription_id": self.subscription_id,
            "event_type": self.event_type,
            "attempt": self.attempt,
            "status": self.status,
            "response_code": self.response_code,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WebhookDelivery id={self.id} status={self.status!r}>"


class WebhookSubscriptionEvent(db.Model):
    """One event-type filter belonging to a subscription (#667)."""

    __tablename__ = "webhook_subscription_events"
    __table_args__ = (
        db.UniqueConstraint(
            "subscription_id",
            "event_type",
            name="uq_webhook_subscription_event",
        ),
        db.Index(
            "ix_webhook_subscription_events_type_subscription",
            "event_type",
            "subscription_id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(100), nullable=False, index=True)

    subscription = db.relationship("WebhookSubscription", back_populates="event_filters")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WebhookSubscriptionEvent id={self.id} event_type={self.event_type!r}>"


class WebhookDeadLetter(db.Model):
    """A permanently failed delivery retained for owner-driven replay (#671)."""

    __tablename__ = "webhook_dead_letters"
    __table_args__ = (
        db.Index(
            "ix_webhook_dead_letters_subscription_created",
            "subscription_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(100), nullable=False)
    #: Serialized JSON payload needed to replay the delivery.
    payload = db.Column(db.Text, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=1)
    response_code = db.Column(db.Integer, nullable=True)
    error = db.Column(db.String(500), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    replayed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    replayed_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    subscription = db.relationship("WebhookSubscription")

    def to_dict(self) -> dict:
        """Serialize metadata; the payload is replayed but never listed."""
        return {
            "id": self.id,
            "subscription_id": self.subscription_id,
            "event_type": self.event_type,
            "attempts": self.attempts,
            "response_code": self.response_code,
            "error": self.error,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "replayed_at": self.replayed_at.isoformat() if self.replayed_at else None,
            "replayed_by_id": self.replayed_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WebhookDeadLetter id={self.id} event_type={self.event_type!r}>"
