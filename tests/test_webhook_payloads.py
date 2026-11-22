"""Boundary tests for webhook payload size limits (#674)."""

from __future__ import annotations

import pytest

from app.services import job_queue
from app.services.webhook_payloads import (
    WebhookPayloadError,
    WebhookPayloadTooLarge,
    payload_size,
    prepare_payload,
)


def test_payload_exactly_at_the_limit_is_accepted(app):
    payload = {"event": "review.completed", "review_id": 7}
    limit = payload_size(payload)
    app.config["WEBHOOK_MAX_PAYLOAD_BYTES"] = limit

    prepared, truncated = prepare_payload(payload)

    assert prepared == payload
    assert truncated == ()
    assert payload_size(prepared) == limit


def test_payload_one_byte_over_the_limit_is_rejected(app):
    payload = {"event": "review.completed", "review_id": 7}
    limit = payload_size(payload) - 1
    app.config["WEBHOOK_MAX_PAYLOAD_BYTES"] = limit

    with pytest.raises(WebhookPayloadTooLarge) as exc:
        prepare_payload(payload)

    assert exc.value.code == "webhook_payload_too_large"
    assert exc.value.size == limit + 1
    assert exc.value.limit == limit


def test_optional_fields_can_be_truncated_but_required_fields_are_untouched(app):
    app.config["WEBHOOK_MAX_PAYLOAD_BYTES"] = 200
    payload = {"event": "review.completed", "required_id": 42, "details": "x" * 2000}

    prepared, truncated = prepare_payload(
        payload,
        truncate_fields=["details"],
        required_fields=["event", "required_id"],
    )

    assert prepared["event"] == "review.completed"
    assert prepared["required_id"] == 42
    assert len(prepared["details"]) < len(payload["details"])
    assert truncated == ("details",)
    assert payload_size(prepared) <= 200


def test_a_required_field_cannot_be_named_for_truncation(app):
    with pytest.raises(WebhookPayloadError):
        prepare_payload(
            {"event": "x", "details": "y" * 1000},
            max_bytes=10,
            truncate_fields=["event"],
            required_fields=["event"],
        )


def test_truncation_that_still_does_not_fit_is_rejected(app):
    app.config["WEBHOOK_MAX_PAYLOAD_BYTES"] = 20
    with pytest.raises(WebhookPayloadTooLarge):
        prepare_payload(
            {"event": "x", "a": "y" * 100, "b": "z" * 100},
            truncate_fields=["a", "b"],
        )


def test_delivery_rejects_oversized_payload_before_network(monkeypatch, app):
    app.config["WEBHOOK_MAX_PAYLOAD_BYTES"] = 64
    posted = []
    monkeypatch.setattr(job_queue, "validate_webhook_url", lambda value: value)
    monkeypatch.setattr(
        job_queue.requests,
        "post",
        lambda *args, **kwargs: posted.append((args, kwargs)),
    )

    with pytest.raises(job_queue.JobQueueError) as exc:
        job_queue.deliver_webhook(
            {
                "url": "https://example.com/hook",
                "payload": {"event": "x", "body": "y" * 500},
            }
        )

    assert "exceeding" in str(exc.value)
    assert posted == []
