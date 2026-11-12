"""Tests for webhook payload size limits, pre-buffering defense, and policies (Phase 12, #674)."""

import io
import json
import logging

import pytest

from app import create_app
from app.services.webhook_payloads import (
    WebhookPayloadTooLargeError,
    enforce_payload_size,
    get_webhook_limits,
    read_webhook_stream,
)


@pytest.fixture
def app():
    """Create test application context with testing config."""
    app = create_app("testing")
    app.config["WEBHOOK_MAX_PAYLOAD_BYTES"] = 1024  # 1 KB limit for testing
    app.config["WEBHOOK_OVERSIZED_POLICY"] = "reject"
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestWebhookPayloadService:
    """Unit tests for payload size enforcement service functions."""

    def test_default_limits_fallback(self):
        limits = get_webhook_limits()
        assert limits.max_payload_bytes > 0
        assert limits.policy in {"reject", "truncate"}

    def test_under_limit_payload_passes(self):
        payload = {"event_type": "project.created", "id": 1, "data": {"name": "test"}}
        result, truncated, fields = enforce_payload_size(payload, max_bytes=1024, policy="reject")
        assert result == payload
        assert truncated is False
        assert fields == []

    def test_at_limit_payload_passes(self):
        base_payload = {"event_type": "project.created", "id": 1, "padding": ""}
        serialized_len = len(json.dumps(base_payload, separators=(",", ":")).encode("utf-8"))
        padding_needed = 200 - serialized_len
        base_payload["padding"] = "a" * padding_needed

        exact_bytes = len(json.dumps(base_payload, separators=(",", ":")).encode("utf-8"))
        result, truncated, _ = enforce_payload_size(
            base_payload, max_bytes=exact_bytes, policy="reject"
        )
        assert result == base_payload
        assert truncated is False

    def test_over_limit_payload_rejected_by_default(self):
        payload = {"event_type": "project.created", "data": {"large_blob": "x" * 2000}}
        with pytest.raises(WebhookPayloadTooLargeError) as exc_info:
            enforce_payload_size(payload, max_bytes=500, policy="reject")
        assert exc_info.value.status_code == 413
        assert exc_info.value.code == "payload_too_large"
        assert exc_info.value.actual_bytes > 500

    def test_over_limit_payload_safe_truncation(self):
        payload = {
            "event_type": "project.updated",
            "id": 42,
            "timestamp": "2026-10-28T00:00:00Z",
            "data": {
                "diff": "diff --git a/file.txt b/file.txt\n" + ("+line\n" * 500),
                "author": "dev",
            },
        }
        truncated_payload, was_truncated, truncated_fields = enforce_payload_size(
            payload, max_bytes=800, policy="truncate"
        )
        assert was_truncated is True
        assert "data.diff" in truncated_fields
        # Required / immutable fields are preserved intact
        assert truncated_payload["event_type"] == "project.updated"
        assert truncated_payload["id"] == 42
        assert truncated_payload["timestamp"] == "2026-10-28T00:00:00Z"
        assert truncated_payload["_truncated"] is True
        assert truncated_payload["_original_size"] > 800

        # Serialized size now fits within limit
        final_size = len(json.dumps(truncated_payload, separators=(",", ":")).encode("utf-8"))
        assert final_size <= 800

    def test_truncation_fails_closed_if_required_fields_exceed_limit(self):
        # Even under truncate policy, required fields are NEVER dropped or corrupted
        large_required_id = "req-" + ("9" * 600)
        payload = {
            "event_type": "project.created",
            "id": large_required_id,
            "timestamp": "2026-10-28T00:00:00Z",
            "data": {"diff": "short diff"},
        }
        with pytest.raises(WebhookPayloadTooLargeError) as exc_info:
            enforce_payload_size(payload, max_bytes=300, policy="truncate")
        assert exc_info.value.status_code == 413
        assert "required" in exc_info.value.message.lower()

    def test_pre_buffering_rejects_content_length_immediately(self):
        stream = io.BytesIO(b"dummy")
        with pytest.raises(WebhookPayloadTooLargeError) as exc:
            read_webhook_stream(stream, content_length=500000, max_bytes=1024)
        assert exc.value.status_code == 413
        # Stream was not read
        assert stream.tell() == 0

    def test_stream_reading_aborts_when_exceeding_max_bytes(self):
        stream = io.BytesIO(b"a" * 2048)
        with pytest.raises(WebhookPayloadTooLargeError) as exc:
            read_webhook_stream(stream, content_length=None, max_bytes=1024, chunk_size=512)
        assert exc.value.status_code == 413
        assert exc.value.actual_bytes > 1024


class TestWebhookEndpoints:
    """Integration tests for webhook HTTP endpoints and boundary checks."""

    def test_get_policy_endpoint(self, client):
        resp = client.get("/api/webhooks/policy")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["max_payload_bytes"] == 1024
        assert data["policy"] == "reject"
        assert "reject" in data["supported_policies"]
        assert "truncate" in data["supported_policies"]

    def test_under_limit_request_accepted(self, client):
        payload = {"event_type": "project.created", "project_id": 123}
        resp = client.post("/api/webhooks/events", json=payload)
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["status"] == "accepted"
        assert data["event_type"] == "project.created"

    def test_over_limit_request_rejected_with_413(self, client):
        payload = {"event_type": "project.created", "large_data": "z" * 2000}
        resp = client.post("/api/webhooks/events", json=payload)
        assert resp.status_code == 413
        data = resp.get_json()
        assert data["code"] == "payload_too_large"
        assert "exceeds" in data["error"].lower()

    def test_over_limit_request_with_truncate_policy(self, app, client):
        app.config["WEBHOOK_OVERSIZED_POLICY"] = "truncate"
        payload = {
            "event_type": "review.completed",
            "review_id": 99,
            "data": {
                "findings": "f" * 1500,
                "summary": "completed",
            },
        }
        resp = client.post("/api/webhooks/events", json=payload)
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["status"] == "accepted"
        assert data["truncated"] is True
        assert len(data["truncated_fields"]) > 0

    def test_malformed_json_request(self, client):
        resp = client.post(
            "/api/webhooks/events",
            data="{invalid_json:",
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["code"] == "malformed_payload"

    def test_empty_request_body(self, client):
        resp = client.post(
            "/api/webhooks/events",
            data="",
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["code"] == "empty_payload"

    def test_missing_required_event_type(self, client):
        resp = client.post(
            "/api/webhooks/events",
            json={"data": "no event_type key here"},
        )
        assert resp.status_code == 422
        data = resp.get_json()
        assert data["code"] == "invalid_payload"
        assert "event_type" in data["error"]

    def test_unauthorized_request_when_secret_configured(self, app, client):
        app.config["WEBHOOK_INGESTION_SECRET"] = "super-secret-token"

        # Missing auth header -> 401
        resp = client.post(
            "/api/webhooks/events",
            json={"event_type": "project.created"},
        )
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "unauthorized"

        # Wrong auth header -> 401
        resp2 = client.post(
            "/api/webhooks/events",
            json={"event_type": "project.created"},
            headers={"X-Webhook-Secret": "wrong-secret"},
        )
        assert resp2.status_code == 401
        assert resp2.get_json()["code"] == "unauthorized"

        # Valid auth header -> 202
        resp3 = client.post(
            "/api/webhooks/events",
            json={"event_type": "project.created"},
            headers={"X-Webhook-Secret": "super-secret-token"},
        )
        assert resp3.status_code == 202

    def test_no_sensitive_data_in_logs(self, app, client, caplog):
        app.config["WEBHOOK_INGESTION_SECRET"] = "top-secret-password-123"
        secret_payload = {"event_type": "project.created", "user_password": "super-confidential"}

        with caplog.at_level(logging.INFO):
            client.post(
                "/api/webhooks/events",
                json=secret_payload,
                headers={"X-Webhook-Secret": "top-secret-password-123"},
            )

        log_text = caplog.text
        assert "top-secret-password-123" not in log_text
        assert "super-confidential" not in log_text
