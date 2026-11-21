"""Tests for HMAC webhook signing (#669)."""

from __future__ import annotations

import pytest

from app.services import job_queue, webhook_signing

SECRET = "whsec_known_answer"
TIMESTAMP = 1_700_000_000
BODY = b'{"event":"ping"}'
EXPECTED = "v1=ea1980a87d2594bdf099acaf428e26cd5bb851648cde873453733621f66b1d5e"


def test_known_answer_vector():
    assert webhook_signing.compute_signature(SECRET, BODY, TIMESTAMP) == EXPECTED
    assert webhook_signing.signed_content(TIMESTAMP, BODY) == b'1700000000.{"event":"ping"}'
    headers = webhook_signing.signature_headers(SECRET, BODY, TIMESTAMP)
    assert headers[webhook_signing.SIGNATURE_HEADER] == EXPECTED
    assert headers[webhook_signing.TIMESTAMP_HEADER] == "1700000000"


def test_verify_accepts_fresh_and_rejects_tampered_or_replayed():
    assert webhook_signing.verify_signature(SECRET, BODY, EXPECTED, TIMESTAMP, now=TIMESTAMP)
    assert not webhook_signing.verify_signature(
        SECRET, BODY, EXPECTED, TIMESTAMP, now=TIMESTAMP + 301
    )
    assert not webhook_signing.verify_signature(
        SECRET, b'{"event":"pong"}', EXPECTED, TIMESTAMP, now=TIMESTAMP
    )
    assert not webhook_signing.verify_signature(
        SECRET, BODY, "v1=deadbeef", TIMESTAMP, now=TIMESTAMP
    )
    assert not webhook_signing.verify_signature(
        SECRET, BODY, EXPECTED, TIMESTAMP - 301, now=TIMESTAMP
    )


def test_verify_validates_configuration():
    with pytest.raises(ValueError):
        webhook_signing.compute_signature("", BODY, TIMESTAMP)
    with pytest.raises(TypeError):
        webhook_signing.compute_signature(SECRET, 123, TIMESTAMP)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        webhook_signing.verify_signature(SECRET, BODY, EXPECTED, -1)


def test_extract_signature_is_case_insensitive():
    headers = {
        "x-aica-signature": EXPECTED,
        "X-AICA-TIMESTAMP": str(TIMESTAMP),
    }
    assert webhook_signing.extract_signature(headers) == (EXPECTED, TIMESTAMP)
    assert webhook_signing.extract_signature({}) == (None, None)
    assert webhook_signing.extract_signature({"X-AICA-Timestamp": "nope"}) == (None, None)


def test_delivery_signs_when_a_subscription_is_supplied(app, make_user, db, monkeypatch):
    from app.models import WebhookSubscription, Workspace

    user = make_user()
    workspace = Workspace(user_id=user.id, name="Demo", slug="demo")
    db.session.add(workspace)
    db.session.flush()
    subscription = WebhookSubscription(
        workspace_id=workspace.id,
        url="https://example.com/hook",
        is_active=True,
    )
    subscription.set_secret("whsec_delivery")
    subscription.set_events(["analysis.completed"])
    db.session.add(subscription)
    db.session.commit()

    captured: dict = {}

    class _Response:
        status_code = 204

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _Response()

    monkeypatch.setattr(job_queue.requests, "post", fake_post)
    monkeypatch.setattr(job_queue, "validate_webhook_url", lambda url: url)

    result = job_queue.deliver_webhook(
        {
            "url": "https://example.com/hook",
            "payload": {"event": "analysis.completed", "ok": True},
            "subscription_id": subscription.id,
            "event": "analysis.completed",
        }
    )
    assert result == {"status_code": 204}
    headers = captured["headers"]
    signature = headers[webhook_signing.SIGNATURE_HEADER]
    timestamp = int(headers[webhook_signing.TIMESTAMP_HEADER])
    assert webhook_signing.verify_signature(
        "whsec_delivery", captured["data"], signature, timestamp
    )
    assert captured["data"] == b'{"event":"analysis.completed","ok":true}'


def test_delivery_rejects_mismatched_event(app, make_user, db, monkeypatch):
    from app.models import WebhookSubscription, Workspace

    user = make_user()
    workspace = Workspace(user_id=user.id, name="Demo", slug="demo")
    db.session.add(workspace)
    db.session.flush()
    subscription = WebhookSubscription(
        workspace_id=workspace.id,
        url="https://example.com/hook",
        is_active=True,
    )
    subscription.set_secret("whsec_delivery")
    subscription.set_events(["analysis.completed"])
    db.session.add(subscription)
    db.session.commit()

    monkeypatch.setattr(job_queue, "validate_webhook_url", lambda url: url)
    with pytest.raises(job_queue.JobQueueError):
        job_queue.deliver_webhook(
            {
                "url": "https://example.com/hook",
                "payload": {},
                "subscription_id": subscription.id,
                "event": "review.completed",
            }
        )
