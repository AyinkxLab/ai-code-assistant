"""POST idempotency key tests (#649)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import IdempotencyKey, Workspace, WorkspaceDomain
from app.services.idempotency import (
    HEADER,
    REPLAY_HEADER,
    InvalidKey,
    canonical_request_hash,
    purge_expired,
    validate_key,
)


def _workspace(user_id, name="Acme"):
    workspace = Workspace(user_id=user_id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestKeyValidation:
    @pytest.mark.parametrize(
        "value",
        ["abc", "550e8400-e29b-41d4-a716-446655440000", "retry_1:webhooks/orders"],
    )
    def test_accepts_reasonable_keys(self, value):
        assert validate_key(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "has space", "line\nbreak", "x" * 256, "semi;colon"],
    )
    def test_rejects_malformed_keys(self, value):
        with pytest.raises(InvalidKey):
            validate_key(value)

    def test_json_hash_is_order_insensitive(self):
        first = canonical_request_hash(b'{"a": 1, "b": [2, 3]}', "application/json")
        second = canonical_request_hash(b'{"b": [2, 3], "a": 1}', "application/json")
        assert first == second

    def test_different_bodies_have_different_hashes(self):
        assert canonical_request_hash(b'{"a": 1}', "application/json") != (
            canonical_request_hash(b'{"a": 2}', "application/json")
        )


class TestPostIdempotencyRoutes:
    def test_repeated_key_replays_the_original_response(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)
        headers = {HEADER: "challenge-key-1"}
        payload = {"workspace_id": workspace.id, "domain": "example.com"}

        first = client.post("/auth/api/domains/challenge", json=payload, headers=headers)
        second = client.post("/auth/api/domains/challenge", json=payload, headers=headers)

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.get_json() == first.get_json()
        assert second.headers[REPLAY_HEADER] == "true"
        assert (
            WorkspaceDomain.query.filter_by(workspace_id=workspace.id, domain="example.com").count()
            == 1
        )

    def test_same_key_with_a_different_body_is_rejected(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)
        headers = {HEADER: "challenge-key-2"}

        first = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "example.com"},
            headers=headers,
        )
        second = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "other.example"},
            headers=headers,
        )

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.get_json()["code"] == "conflict"

    def test_invalid_key_gets_a_structured_error(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)

        response = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "example.com"},
            headers={HEADER: "bad key"},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_in_progress_key_is_rejected_safely(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)
        payload = {"workspace_id": workspace.id, "domain": "example.com"}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        row = IdempotencyKey(
            owner=f"user:{user.id}",
            user_id=user.id,
            endpoint="/auth/api/domains/challenge",
            key="in-progress-key",
            request_hash=canonical_request_hash(raw, "application/json"),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db.session.add(row)
        db.session.commit()

        response = client.post(
            "/auth/api/domains/challenge",
            json=payload,
            headers={HEADER: "in-progress-key"},
        )

        assert response.status_code == 409
        assert response.get_json()["code"] == "conflict"
        assert "progress" in response.get_json()["error"].lower()

    def test_expired_key_is_purged_and_can_be_used_again(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)
        row = IdempotencyKey(
            owner=f"user:{user.id}",
            user_id=user.id,
            endpoint="/auth/api/domains/challenge",
            key="expired-key",
            request_hash="old",
            status_code=500,
            response_body="stale",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        db.session.add(row)
        db.session.commit()

        response = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "example.com"},
            headers={HEADER: "expired-key"},
        )

        assert response.status_code == 201
        assert IdempotencyKey.query.filter_by(key="expired-key").one().status_code == 201

    def test_purge_expired_removes_only_expired_rows(self, db):
        expired = IdempotencyKey(
            owner="anonymous",
            endpoint="/x",
            key="expired",
            request_hash="h",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        live = IdempotencyKey(
            owner="anonymous",
            endpoint="/x",
            key="live",
            request_hash="h",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.session.add_all([expired, live])
        db.session.commit()

        assert purge_expired() == 1
        assert IdempotencyKey.query.filter_by(key="expired").first() is None
        assert IdempotencyKey.query.filter_by(key="live").first() is not None
