"""Tests for the Slack notification connector (#678)."""

from __future__ import annotations

import pytest
import requests

from app.models import IntegrationCredential, Workspace
from app.services.integration_credentials import decrypt_for_use
from app.services.integrations import health, slack
from app.services.integrations.common import (
    IntegrationDeliveryError,
    IntegrationNotConfigured,
    IntegrationValidationError,
)

WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret-token"


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture()
def workspace(db, make_user):
    owner = make_user(username="owner", email="owner@example.com")
    row = Workspace(user_id=owner.id, name="Slack Workspace")
    db.session.add(row)
    db.session.commit()
    return row, owner


def _configure(monkeypatch, workspace, owner, url=WEBHOOK):
    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value)
    slack.configure(workspace.id, owner, url)


def test_configure_stores_encrypted_secret(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)

    credential = IntegrationCredential.query.filter_by(
        workspace_id=row.id, integration="slack", name="webhook_url"
    ).one()
    assert credential.encrypted_value != WEBHOOK
    assert WEBHOOK not in credential.encrypted_value
    assert decrypt_for_use(credential) == WEBHOOK


def test_configure_rejects_invalid_url(monkeypatch, workspace):
    row, owner = workspace

    def reject(_value):
        from app.services.job_queue import JobQueueError

        raise JobQueueError("webhook host must be publicly routable")

    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", reject)
    with pytest.raises(IntegrationValidationError):
        slack.configure(row.id, owner, "http://127.0.0.1/hook")


def test_build_payload_is_bounded_and_secret_free():
    from app.services.integrations.common import build_event_summary

    summary = build_event_summary(
        "review.completed",
        {
            "review_id": 1,
            "kind": "security",
            "status": "completed",
            "secret": "do-not-leak",
            "body": "x" * 10000,
        },
    )
    payload = slack.build_payload(summary)
    assert len(payload["text"]) <= slack.MAX_TEXT_CHARS
    assert summary["title"] in payload["text"]
    assert "do-not-leak" not in payload["text"]
    assert "x" * 2000 not in payload["text"]


def test_send_event_posts_bounded_payload_and_records_health(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(200)

    result = slack.send_event(
        row,
        "review.completed",
        {"review_id": 7, "kind": "security", "status": "completed", "token": "nope"},
        post=fake_post,
        sleep=lambda _delay: None,
    )

    assert result["status"] == "sent"
    assert result["status_code"] == 200
    assert calls[0][0] == WEBHOOK
    assert calls[0][1]["allow_redirects"] is False
    text = calls[0][1]["json"]["text"]
    assert "Code review completed" in text
    assert "nope" not in text

    snapshot = health.get_health(row.id, "slack")
    assert snapshot["status"] == "healthy"
    assert snapshot["last_success_at"] is not None


def test_transient_failures_are_retried_with_backoff(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    attempts = []
    delays = []

    def flaky(url, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise requests.ConnectionError(f"failed to reach {WEBHOOK}")
        return _Response(200)

    result = slack.send_event(
        row,
        "budget.alert",
        {"kind": "over_budget", "spend": 110, "limit": 100},
        post=flaky,
        sleep=delays.append,
    )

    assert result["status"] == "sent"
    assert len(attempts) == 3
    assert len(delays) == 2
    assert delays == sorted(delays)


def test_client_error_is_not_retried_and_error_is_redacted(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    calls = []

    def reject(url, **kwargs):
        calls.append(1)
        return _Response(400)

    with pytest.raises(IntegrationDeliveryError):
        slack.send_event(
            row,
            "security.alert",
            {"title": "Finding", "summary": "A finding."},
            post=reject,
            sleep=lambda _delay: None,
        )

    assert len(calls) == 1
    snapshot = health.get_health(row.id, "slack")
    assert snapshot["status"] == "error"
    assert snapshot["last_error_code"] == "delivery_failed"
    assert WEBHOOK not in (snapshot["last_error"] or "")


def test_server_error_exhausts_retries_and_records_failure(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    calls = []

    def unavailable(url, **kwargs):
        calls.append(1)
        return _Response(503)

    with pytest.raises(IntegrationDeliveryError):
        slack.send_event(
            row,
            "review.completed",
            {"review_id": 1},
            post=unavailable,
            sleep=lambda _delay: None,
        )

    assert len(calls) == 4
    assert health.get_health(row.id, "slack")["last_status_code"] == 503


def test_unconfigured_and_unknown_events_are_structured(workspace):
    row, _owner = workspace
    with pytest.raises(IntegrationNotConfigured):
        slack.send_event(row, "review.completed", {})

    with pytest.raises(IntegrationValidationError):
        slack.send_event(row, "unknown.event", {})


def test_test_connection_sends_test_message(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    payloads = []

    def fake_post(url, **kwargs):
        payloads.append(kwargs["json"])
        return _Response(200)

    result = slack.test_connection(row, post=fake_post, sleep=lambda _delay: None)
    assert result["event_type"] == "test"
    assert "Test notification" in payloads[0]["text"]
