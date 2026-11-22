"""Compatibility import for the idempotency key model (#649)."""

from app.models.idempotency_key import IdempotencyKey

__all__ = ["IdempotencyKey"]
