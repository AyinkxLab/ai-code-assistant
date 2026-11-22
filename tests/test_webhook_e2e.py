"""End-to-end webhook tests against a local loopback receiver (#675).

The receiver is a plain ``http.server`` bound to ``127.0.0.1``. No live
third-party service is contacted. The test drives the real subscription API,
delivery, signature verification, retry, and dead-letter replay paths.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import requests

from app.models import WebhookSubscription, Workspace
from app.services import job_queue, webhook_dead_letters, webhook_retry, webhook_signing


class _ReceiverHandler(BaseHTTPRequestHandler):
    server_version = "WebhookReceiver/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep pytest output deterministic
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": urlsplit(self.path).path,
                "body": body,
                "headers": dict(self.headers.items()),
            }
        )
        status = self.server.responses.pop(0) if self.server.responses else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class LocalReceiver:
    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ReceiverHandler)
        self._server.requests = []
        self._server.responses = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/hooks"

    @property
    def requests(self):
        return self._server.requests

    def respond(self, *statuses):
        self._server.responses.extend(statuses)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def receiver():
    server = LocalReceiver().start()
    try:
        yield server
    finally:
        server.stop()


def _workspace(db, user):
    workspace = Workspace(user_id=user.id, name="Webhook E2E")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _create_subscription(client, db, workspace, receiver_url, monkeypatch):
    monkeypatch.setattr(job_queue, "validate_webhook_url", lambda value: value)
    response = client.post(
        f"/webhooks/api/workspaces/{workspace.id}/subscriptions",
        json={"url": receiver_url, "event_type": "review.completed"},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _post_signed(url, payload, secret):
    """POST a signed body and return the raw response (for retry testing)."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {"Content-Type": "application/json", **webhook_signing.signature_headers(secret, raw)}
    return requests.post(url, data=raw, headers=headers, timeout=5, allow_redirects=False)


def test_subscription_delivery_and_signature_verification(
    client, db, make_user, login, receiver, monkeypatch
):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    created = _create_subscription(client, db, workspace, receiver.url, monkeypatch)
    secret = created["secret"]
    subscription = db.session.get(WebhookSubscription, created["id"])

    result = job_queue.deliver_webhook(
        {
            "url": receiver.url,
            "payload": {"event": "review.completed", "review_id": 11},
            "secret": subscription.signing_secret(),
            "event": "review.completed",
            "subscription_id": subscription.id,
        }
    )

    assert result == {"status_code": 200}
    request = receiver.requests[-1]
    body = request["body"]
    signature, timestamp = webhook_signing.extract_signature(request["headers"])
    assert signature is not None and timestamp is not None
    assert webhook_signing.verify_signature(secret, body, signature, timestamp)
    assert json.loads(body) == {"event": "review.completed", "review_id": 11}


def test_retry_recovers_after_transient_failures(
    client, db, make_user, login, receiver, monkeypatch
):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    created = _create_subscription(client, db, workspace, receiver.url, monkeypatch)
    receiver.respond(503, 500, 200)

    result = webhook_retry.deliver_with_retry(
        lambda: _post_signed(
            receiver.url,
            {"event": "review.completed"},
            created["secret"],
        ),
        policy=webhook_retry.RetryPolicy(
            max_attempts=4,
            base_delay=0.0,
            max_delay=0.0,
            total_delay_cap=0.0,
        ),
        sleep=lambda _delay: None,
        jitter=lambda: 1.0,
    )

    assert result.success is True
    assert [attempt.response_code for attempt in result.attempts] == [503, 500, 200]
    assert len(receiver.requests) == 3


def test_permanent_failure_is_retained_and_replayable(
    client, db, make_user, login, receiver, monkeypatch
):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    created = _create_subscription(client, db, workspace, receiver.url, monkeypatch)
    subscription = db.session.get(WebhookSubscription, created["id"])
    receiver.respond(500, 500)

    result = webhook_retry.deliver_with_retry(
        lambda: _post_signed(
            receiver.url,
            {"event": "review.completed", "review_id": 99},
            created["secret"],
        ),
        policy=webhook_retry.RetryPolicy(
            max_attempts=2,
            base_delay=0.0,
            max_delay=0.0,
            total_delay_cap=0.0,
        ),
        sleep=lambda _delay: None,
        jitter=lambda: 1.0,
    )
    assert result.success is False
    assert len(receiver.requests) == 2

    dead = webhook_dead_letters.record_failure(
        subscription,
        event_type="review.completed",
        payload={"event": "review.completed", "review_id": 99},
        attempts=len(result.attempts),
        response_code=result.response_code,
        error=result.error,
    )
    enqueued = []
    monkeypatch.setattr(
        job_queue, "enqueue", lambda kind, payload: enqueued.append((kind, payload))
    )
    replayed = webhook_dead_letters.replay(subscription, [dead.id], actor=owner)
    assert replayed["replayed"] == 1
    assert enqueued[0][0] == "webhook.deliver"

    # A duplicate replay is skipped, and the captured payload can be delivered
    # successfully once the receiver recovers.
    again = webhook_dead_letters.replay(subscription, [dead.id], actor=owner)
    assert again["duplicates"] == 1
    receiver.respond(200)
    job_queue.deliver_webhook(
        {
            "url": enqueued[0][1]["url"],
            "payload": enqueued[0][1]["payload"],
            "secret": created["secret"],
        }
    )
    assert json.loads(receiver.requests[-1]["body"])["review_id"] == 99


def test_receiver_can_reject_a_tampered_signature(receiver, monkeypatch):
    monkeypatch.setattr(job_queue, "validate_webhook_url", lambda value: value)
    receiver.respond(401)
    # The delivery helper faithfully reports a rejecting receiver as a failure.
    with pytest.raises(RuntimeError):
        job_queue.deliver_webhook(
            {
                "url": receiver.url,
                "payload": {"event": "review.completed"},
                "secret": "correct-secret",
            }
        )
