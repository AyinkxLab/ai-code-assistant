"""Tests for the inbound/outbound email integration (#682)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from app.models import EmailSuppression, Notification, Workspace
from app.services.integrations import email as email_integration
from app.services.integrations.common import IntegrationValidationError
from app.services.integrations.email import InboundEmailError

SECRET = "inbound-secret-value-0123456789"


@pytest.fixture()
def workspace(db, make_user):
    owner = make_user(username="owner", email="owner@example.com")
    row = Workspace(user_id=owner.id, name="Email Workspace")
    db.session.add(row)
    db.session.commit()
    return row, owner


def _sign(secret, timestamp, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("ascii") + body,
        hashlib.sha256,
    ).hexdigest()


class _CaptureSender:
    def __init__(self, delivered=True):
        self.delivered = delivered
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.delivered


def test_configure_validates_and_stores_secrets(workspace):
    row, owner = workspace
    with pytest.raises(IntegrationValidationError):
        email_integration.configure(row.id, owner, inbound_secret="short")
    with pytest.raises(IntegrationValidationError):
        email_integration.configure(row.id, owner, reply_to="not-an-email")

    result = email_integration.configure(
        row.id, owner, inbound_secret=SECRET, reply_to="reply@example.com"
    )
    assert result["stored"] == ["inbound_secret", "reply_to"]
    assert email_integration.is_suppressed(row.id, owner.email) is False


def test_build_message_includes_unsubscribe(workspace):
    row, _owner = workspace
    message = email_integration.build_message(
        "budget.alert",
        {"kind": "over_budget", "spend": 110, "limit": 100},
        workspace_name=row.name,
        unsubscribe_url_value="https://app.example.com/unsubscribe",
    )
    assert row.name in message["subject"]
    assert "Budget alert" in message["subject"]
    assert "https://app.example.com/unsubscribe" in message["text"]
    assert "Unsubscribe" in message["html"]


def test_send_event_uses_reply_to_and_unsubscribe_headers(workspace):
    row, owner = workspace
    email_integration.configure(row.id, owner, reply_to="support@example.com")
    sender = _CaptureSender()

    result = email_integration.send_event(
        row,
        "review.completed",
        {"review_id": 12, "kind": "quality", "status": "completed"},
        user=owner,
        sender=sender,
    )

    assert result["status"] == "sent"
    assert result["reply_to"] == "support@example.com"
    call = sender.calls[0]
    assert call["to"] == owner.email
    assert call["reply_to"] == "support@example.com"
    assert call["headers"]["List-Unsubscribe"].startswith("<")
    assert "Review 12" in call["text_body"]


def test_suppressed_recipient_is_skipped(workspace):
    row, owner = workspace
    email_integration.suppress(row.id, owner.email)
    sender = _CaptureSender()

    result = email_integration.send_event(
        row, "review.completed", {"review_id": 1}, user=owner, sender=sender
    )

    assert result["status"] == "skipped"
    assert sender.calls == []


def test_failed_delivery_is_skipped_and_recorded(workspace):
    row, owner = workspace
    sender = _CaptureSender(delivered=False)

    result = email_integration.send_event(
        row, "review.completed", {"review_id": 1}, user=owner, sender=sender
    )

    assert result["status"] == "skipped"
    assert result["code"] == "delivery_failed"
    snapshot = email_integration.health.get_health(row.id, "email")
    assert snapshot["status"] == "error"


def test_invalid_recipient_is_rejected(workspace):
    row, _owner = workspace
    with pytest.raises(IntegrationValidationError):
        email_integration.send_event(
            row, "review.completed", {}, to="not-an-email", sender=_CaptureSender()
        )


def test_unsubscribe_token_round_trip_and_tampering(workspace):
    row, owner = workspace
    token = email_integration.make_unsubscribe_token(row.id, owner.email)
    assert email_integration.verify_unsubscribe_token(token) == (row.id, owner.email)

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(InboundEmailError) as exc:
        email_integration.verify_unsubscribe_token(tampered)
    assert exc.value.code == "invalid_token"

    result = email_integration.unsubscribe(token)
    assert result["status"] == "unsubscribed"
    assert EmailSuppression.query.filter_by(workspace_id=row.id, email=owner.email).count() == 1


def test_signature_verification_rejects_tampering_and_staleness():
    body = b'{"from":"a@example.com"}'
    timestamp = 1_700_000_000
    good = _sign(SECRET, timestamp, body)

    assert email_integration.verify_inbound_signature(body, timestamp, good, SECRET, now=timestamp)

    with pytest.raises(InboundEmailError) as exc:
        email_integration.verify_inbound_signature(
            body, timestamp, "deadbeef", SECRET, now=timestamp
        )
    assert exc.value.code == "invalid_signature"

    with pytest.raises(InboundEmailError) as exc:
        email_integration.verify_inbound_signature(
            body, timestamp, good, SECRET, now=timestamp + 3600
        )
    assert exc.value.code == "expired_signature"


def test_parse_inbound_accepts_safe_attachments():
    parsed = email_integration.parse_inbound(
        {
            "from": "Sender@Example.com",
            "subject": "Re: review",
            "text": "hello",
            "attachments": [
                {"filename": "report.pdf", "content_type": "application/pdf", "size": 1200}
            ],
        }
    )
    assert parsed["from"] == "sender@example.com"
    assert parsed["attachment_count"] == 1
    assert parsed["attachments"][0]["filename"] == "report.pdf"


@pytest.mark.parametrize(
    "attachment",
    [
        {"filename": "malware.exe", "size": 10},
        {"filename": "script.ps1", "size": 10},
        {"filename": "../secret.txt", "size": 10},
        {"filename": "ok.txt", "size": 11 * 1024 * 1024},
    ],
)
def test_parse_inbound_rejects_unsafe_attachments(attachment):
    with pytest.raises(InboundEmailError) as exc:
        email_integration.parse_inbound(
            {"from": "a@example.com", "subject": "x", "attachments": [attachment]}
        )
    assert exc.value.code == "unsafe_attachment"


def test_parse_inbound_bounds_attachment_count():
    attachments = [{"filename": f"file-{index}.txt", "size": 1} for index in range(6)]
    with pytest.raises(InboundEmailError) as exc:
        email_integration.parse_inbound(
            {"from": "a@example.com", "subject": "x", "attachments": attachments}
        )
    assert exc.value.code == "unsafe_attachment"


def test_process_inbound_creates_notification(workspace):
    row, owner = workspace
    email_integration.configure(row.id, owner, inbound_secret=SECRET)
    body = json.dumps(
        {"from": "sender@example.com", "subject": "Re: review", "text": "thanks"}
    ).encode("utf-8")
    timestamp = int(time.time())
    signature = _sign(SECRET, timestamp, body)
    before = Notification.query.filter_by(user_id=owner.id).count()

    result = email_integration.process_inbound(row.id, body, timestamp, signature, now=timestamp)

    assert result["status"] == "accepted"
    assert Notification.query.filter_by(user_id=owner.id).count() == before + 1


def test_process_inbound_rejects_bad_signature(workspace):
    row, owner = workspace
    email_integration.configure(row.id, owner, inbound_secret=SECRET)
    body = b'{"from":"sender@example.com"}'
    timestamp = int(time.time())

    with pytest.raises(InboundEmailError) as exc:
        email_integration.process_inbound(row.id, body, timestamp, "bad", now=timestamp)
    assert exc.value.code == "invalid_signature"


def test_inbound_route_accepts_signed_mail(client, workspace):
    row, owner = workspace
    email_integration.configure(row.id, owner, inbound_secret=SECRET)
    body = json.dumps({"from": "sender@example.com", "subject": "Re", "text": "hi"}).encode()
    timestamp = int(time.time())
    response = client.post(
        f"/integrations/email/{row.id}/inbound",
        data=body,
        headers={
            "X-Timestamp": str(timestamp),
            "X-Signature": _sign(SECRET, timestamp, body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.get_json()["status"] == "accepted"
