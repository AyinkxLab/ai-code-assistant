"""SIEM webhook forwarding tests (#629)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.models import SiemEndpoint, Workspace
from app.services import job_queue, siem, webhook_retry, webhook_signing

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _workspace(db, user, name="Security"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _configure(
    db, workspace, owner, monkeypatch, *, secret="whsec_siem", url="https://siem.example/hook"
):
    monkeypatch.setattr(job_queue, "validate_webhook_url", lambda value: value.strip())
    endpoint, raw = siem.configure_endpoint(
        workspace.id,
        url=url,
        secret=secret,
        actor=owner,
    )
    return endpoint, raw


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class TestSiemConfiguration:
    def test_owner_configures_endpoint_and_secret_is_returned_once(
        self, client, db, make_user, login, monkeypatch
    ):
        owner = make_user()
        login()
        workspace = _workspace(db, owner)
        monkeypatch.setattr(job_queue, "validate_webhook_url", lambda value: value.strip())
        base = f"/webhooks/api/workspaces/{workspace.id}/siem"

        created = client.put(base, json={"url": " https://siem.example/hook "})

        assert created.status_code == 201
        body = created.get_json()
        assert body["secret"]
        assert body["url"] == "https://siem.example/hook"
        assert body["is_active"] is True

        listed = client.get(base).get_json()
        assert listed["configured"] is True
        assert listed["endpoint"]["has_secret"] is True
        assert "secret" not in listed["endpoint"]
        assert db.session.get(SiemEndpoint, body["id"]).secret == body["secret"]

    def test_invalid_url_is_structured(self, client, db, make_user, login, monkeypatch):
        owner = make_user()
        login()
        workspace = _workspace(db, owner)

        def reject(_value):
            raise job_queue.JobQueueError("webhook host must be publicly routable")

        monkeypatch.setattr(job_queue, "validate_webhook_url", reject)
        response = client.put(
            f"/webhooks/api/workspaces/{workspace.id}/siem",
            json={"url": "http://127.0.0.1/hook"},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_non_owner_gets_structured_not_found(self, client, db, make_user, login):
        owner = make_user()
        make_user(username="other", email="other@example.com")
        workspace = _workspace(db, owner)
        login(email="other@example.com")

        response = client.put(
            f"/webhooks/api/workspaces/{workspace.id}/siem",
            json={"url": "https://siem.example/hook"},
        )

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"

    def test_delete_removes_endpoint(self, client, db, make_user, login, monkeypatch):
        owner = make_user()
        login()
        workspace = _workspace(db, owner)
        monkeypatch.setattr(job_queue, "validate_webhook_url", lambda value: value.strip())
        base = f"/webhooks/api/workspaces/{workspace.id}/siem"
        assert client.put(base, json={"url": "https://siem.example/hook"}).status_code == 201

        assert client.delete(base).status_code == 200
        assert client.get(base).get_json() == {"configured": False, "endpoint": None}


class TestSiemForwarding:
    def test_payload_is_signed_and_excludes_secrets_and_prompt_content(
        self, app, db, make_user, monkeypatch
    ):
        owner = make_user()
        workspace = _workspace(db, owner)
        _endpoint, secret = _configure(db, workspace, owner, monkeypatch)
        calls = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            return _Response(202)

        result = siem.forward_security_event(
            workspace.id,
            "security.login_failure",
            details={
                "action": "login_failure",
                "count": 2,
                "secret": "sk_live_do_not_forward",
                "prompt": "private prompt content",
                "nested": {"prompt": "nested secret"},
            },
            actor_id=owner.id,
            target_type="user",
            target_id=owner.id,
            now=NOW,
            post=fake_post,
        )

        assert result["delivered"] is True
        assert result["response_code"] == 202
        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == "https://siem.example/hook"
        assert call["allow_redirects"] is False

        raw = call["data"]
        signature = call["headers"][webhook_signing.SIGNATURE_HEADER]
        timestamp = int(NOW.timestamp())
        assert webhook_signing.verify_signature(secret, raw, signature, timestamp, now=timestamp)

        body = raw.decode("utf-8")
        assert "sk_live_do_not_forward" not in body
        assert "private prompt content" not in body
        assert "nested secret" not in body
        event = json.loads(body)
        assert event["event_type"] == "security.login_failure"
        assert event["details"]["secret"] == "[redacted:pii]"
        assert event["details"]["prompt"] == "[redacted:confidential]"
        assert event["details"]["nested"] == "[redacted:confidential]"
        assert event["details"]["count"] == 2

    def test_retries_failures_then_succeeds(self, app, db, make_user, monkeypatch):
        owner = make_user()
        workspace = _workspace(db, owner)
        _configure(db, workspace, owner, monkeypatch)
        statuses = [500, 503, 204]

        def fake_post(url, **kwargs):
            return _Response(statuses.pop(0))

        result = siem.forward_security_event(
            workspace.id,
            "security.audit_export",
            now=NOW,
            post=fake_post,
            policy=webhook_retry.RetryPolicy(max_attempts=3, base_delay=0),
        )

        assert result["delivered"] is True
        assert [attempt["status"] for attempt in result["attempts"]] == [
            "failure",
            "failure",
            "success",
        ]

    def test_retries_are_bounded_and_report_only_labels(self, app, db, make_user, monkeypatch):
        owner = make_user()
        workspace = _workspace(db, owner)
        _configure(db, workspace, owner, monkeypatch)

        def fake_post(url, **kwargs):
            return _Response(500)

        result = siem.forward_security_event(
            workspace.id,
            "security.permission_denied",
            details={"secret": "never-returned"},
            now=NOW,
            post=fake_post,
            policy=webhook_retry.RetryPolicy(max_attempts=2, base_delay=0),
        )

        assert result["delivered"] is False
        assert result["error"] == "HTTP 500"
        assert len(result["attempts"]) == 2
        assert "never-returned" not in json.dumps(result)

    def test_unconfigured_and_inactive_endpoints_do_not_send(self, app, db, make_user, monkeypatch):
        owner = make_user()
        workspace = _workspace(db, owner)

        assert siem.forward_security_event(workspace.id, "security.test")["reason"] == (
            "not_configured"
        )

        endpoint, _secret = _configure(db, workspace, owner, monkeypatch)
        endpoint.is_active = False
        db.session.commit()
        assert siem.forward_security_event(workspace.id, "security.test")["reason"] == "inactive"

    def test_event_type_must_be_a_security_event(self, app, db, make_user, monkeypatch):
        owner = make_user()
        workspace = _workspace(db, owner)
        _configure(db, workspace, owner, monkeypatch)

        with pytest.raises(ValueError):
            siem.forward_security_event(workspace.id, "login_failure")
