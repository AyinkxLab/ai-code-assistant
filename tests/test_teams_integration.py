"""Tests for the Microsoft Teams notification connector (#679)."""

from __future__ import annotations

import pytest
import requests

from app.models import IntegrationCredential, Workspace
from app.services.integration_credentials import decrypt_for_use
from app.services.integrations import health, teams
from app.services.integrations.common import (
    IntegrationDeliveryError,
    IntegrationNotConfigured,
    IntegrationValidationError,
)

WEBHOOK = "https://outlook.office.com/webhook/tenant-id/secret"


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture()
def workspace(db, make_user):
    owner = make_user(username="owner", email="owner@example.com")
    row = Workspace(user_id=owner.id, name="Teams Workspace")
    db.session.add(row)
    db.session.commit()
    return row, owner


def _configure(monkeypatch, workspace, owner, url=WEBHOOK):
    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value)
    teams.configure(workspace.id, owner, url)


def test_configure_uses_ssrf_guard_and_encrypts(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    credential = IntegrationCredential.query.filter_by(
        workspace_id=row.id, integration="teams", name="webhook_url"
    ).one()
    assert credential.encrypted_value != WEBHOOK
    assert decrypt_for_use(credential) == WEBHOOK


def test_configure_rejects_a_private_host(monkeypatch, workspace):
    row, owner = workspace

    def reject(_value):
        from app.services.job_queue import JobQueueError

        raise JobQueueError("webhook host must be publicly routable")

    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", reject)
    with pytest.raises(IntegrationValidationError):
        teams.configure(row.id, owner, "http://10.0.0.1/hook")


def test_payload_is_a_bounded_message_card():
    from app.services.integrations.common import build_event_summary

    summary = build_event_summary(
        "budget.alert",
        {
            "kind": "over_budget",
            "spend": 110,
            "limit": 100,
            "secret": "x" * 10000,
        },
    )
    payload = teams.build_payload(summary)
    assert payload["@type"] == "MessageCard"
    assert payload["title"] == "Budget alert"
    assert len(payload["text"]) <= teams.MAX_TEXT_CHARS
    assert "x" * 2000 not in payload["text"]


def test_send_event_success_and_health(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(200)

    result = teams.send_event(
        row,
        "security.alert",
        {"title": "Critical finding", "summary": "A critical finding was found.", "count": 1},
        post=fake_post,
        sleep=lambda _delay: None,
    )

    assert result["status"] == "sent"
    assert calls[0][1]["json"]["title"] == "Critical finding"
    assert calls[0][1]["allow_redirects"] is False
    assert health.get_health(row.id, "teams")["status"] == "healthy"


def test_retry_then_failure_records_sanitized_error(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    attempts = []

    def flaky(url, **kwargs):
        attempts.append(1)
        raise requests.Timeout(f"timeout posting to {WEBHOOK}")

    with pytest.raises(IntegrationDeliveryError):
        teams.send_event(
            row,
            "review.completed",
            {"review_id": 2},
            post=flaky,
            sleep=lambda _delay: None,
        )

    assert len(attempts) == 4
    snapshot = health.get_health(row.id, "teams")
    assert snapshot["status"] == "error"
    assert WEBHOOK not in (snapshot["last_error"] or "")


def test_unconfigured_and_invalid_event(workspace):
    row, _owner = workspace
    with pytest.raises(IntegrationNotConfigured):
        teams.send_event(row, "review.completed", {})
    with pytest.raises(IntegrationValidationError):
        teams.send_event(row, "nope", {})


def test_test_connection(monkeypatch, workspace):
    row, owner = workspace
    _configure(monkeypatch, row, owner)
    result = teams.test_connection(
        row,
        post=lambda url, **kwargs: _Response(200),
        sleep=lambda _delay: None,
    )
    assert result["event_type"] == "test"
