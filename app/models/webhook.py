"""Webhook subscriptions and delivery logs (Phase 12, #670 and #672).

A subscription belongs to a workspace and targets a public HTTP(S) endpoint. A
delivery row records one attempt: its outcome, HTTP response code, latency, and
a short error label. Delivery rows deliberately never store the response body
or the subscription secret, so the delivery-log UI cannot leak them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

DELIVERY_SUCCESS = "success"
DELIVERY_FAILURE = "failure"
DELIVERY_STATUSES = (DELIVERY_SUCCESS, DELIVERY_FAILURE)


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

    def to_dict(self) -> dict:
        """Serialize the subscription without its secret or full URL."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "event_type": self.event_type,
            "is_active": bool(self.is_active),
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
