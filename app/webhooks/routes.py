"""Webhook ingestion and policy endpoints (Phase 12, #674)."""

from __future__ import annotations

import logging
from typing import Any

from flask import current_app, jsonify, request

from app.services.webhook_payloads import (
    ALLOWED_OVERSIZED_POLICIES,
    get_webhook_limits,
    validate_webhook_request,
)
from app.webhooks import bp

logger = logging.getLogger(__name__)


@bp.route("/policy", methods=["GET"])
def get_policy() -> tuple[Any, int]:
    """Return the currently enforced webhook payload policy and size limits."""
    limits = get_webhook_limits()
    return (
        jsonify(
            {
                "max_payload_bytes": limits.max_payload_bytes,
                "policy": limits.policy,
                "supported_policies": sorted(ALLOWED_OVERSIZED_POLICIES),
            }
        ),
        200,
    )


@bp.route("/events", methods=["POST"])
def ingest_event() -> tuple[Any, int]:
    """Ingest and validate an incoming webhook event.

    Enforces payload size limits, checks JSON structure, verifies required fields,
    and returns consistent structured error responses.
    """
    # 1. Authorization check: if a webhook secret is configured in the environment,
    # require a matching X-Webhook-Secret or Authorization header.
    configured_secret = current_app.config.get("WEBHOOK_INGESTION_SECRET")
    if configured_secret:
        auth_header = request.headers.get("X-Webhook-Secret") or request.headers.get(
            "Authorization"
        )
        if not auth_header:
            return (
                jsonify(
                    {"error": "Missing webhook authentication credentials.", "code": "unauthorized"}
                ),
                401,
            )
        expected = (
            f"Bearer {configured_secret}"
            if not auth_header.startswith("Bearer ")
            else configured_secret
        )
        if auth_header != configured_secret and auth_header != expected:
            return (
                jsonify(
                    {"error": "Invalid webhook authentication credentials.", "code": "unauthorized"}
                ),
                401,
            )

    # 2. Size limit enforcement & pre-buffering stream read
    result = validate_webhook_request(
        request,
        required_fields=["event_type"],
    )
    if not result.is_valid:
        return jsonify(result.to_error_response()), result.status_code

    payload = result.payload or {}
    event_type = payload.get("event_type")

    # Sanitized logging: never log full raw payload or sensitive tokens
    logger.info(
        "Accepted webhook event %s (size: %d bytes, truncated: %s)",
        event_type,
        result.size,
        result.truncated,
    )

    response_data: dict[str, Any] = {
        "status": "accepted",
        "event_type": event_type,
        "size_bytes": result.size,
    }
    if result.truncated:
        response_data["truncated"] = True
        response_data["truncated_fields"] = result.truncated_fields

    return jsonify(response_data), 202
