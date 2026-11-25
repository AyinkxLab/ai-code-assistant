"""API token rotation/revocation UI and audit tests (#617)."""

from __future__ import annotations

import json

from app.extensions import db
from app.models import ApiToken, AuditLog
from app.services import api_tokens, audit

API = "/settings/tokens/api"


def _create(client, **payload):
    payload.setdefault("scopes", ["read"])
    return client.post(API, json=payload)


class TestTokenPage:
    def test_page_requires_login(self, client):
        assert client.get("/settings/tokens/").status_code == 302

    def test_page_renders_without_any_secret(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/settings/tokens/")

        assert response.status_code == 200
        assert b"API tokens" in response.data
        assert b"aat_" not in response.data


class TestTokenMetadata:
    def test_list_shows_metadata_and_never_the_secret(self, client, make_user, login):
        user = make_user()
        login()
        created = _create(client, label="ci", scopes=["read", "write"])
        secret = created.get_json()["secret"]

        listing = client.get(API)
        dumped = json.dumps(listing.get_json())

        assert listing.status_code == 200
        assert secret not in dumped
        row = listing.get_json()[0]
        assert row["label"] == "ci"
        assert row["prefix"] == secret[:12]
        assert row["scopes"] == ["read", "write"]
        assert row["last_used_at"] is None
        assert row["expires_at"] is None
        assert row["revoked_at"] is None
        assert row["is_active"] is True
        assert "token_hash" not in row
        assert ApiToken.query.filter_by(user_id=user.id).count() == 1

    def test_create_returns_the_secret_once(self, client, make_user, login):
        make_user()
        login()

        response = _create(client, label="once", scopes=["read"])

        assert response.status_code == 201
        body = response.get_json()
        assert body["secret"].startswith(api_tokens.SECRET_PREFIX)
        assert body["token"]["label"] == "once"
        assert body["secret"] not in json.dumps(client.get(API).get_json())

    def test_invalid_scopes_return_structured_errors(self, client, make_user, login):
        make_user()
        login()

        unknown = _create(client, scopes=["superuser"])
        over_scoped = _create(client, scopes=["superuser:read"])
        empty = _create(client, scopes=[])

        assert unknown.status_code == 400
        assert unknown.get_json()["code"] == "invalid_input"
        assert over_scoped.status_code == 400
        assert over_scoped.get_json()["code"] == "invalid_input"
        assert empty.status_code == 400
        assert empty.get_json()["code"] == "invalid_input"

    def test_capability_scopes_round_trip_without_secret(self, client, make_user, login):
        make_user()
        login()

        created = _create(client, scopes=["project:read"]).get_json()
        secret = created["secret"]
        listing = client.get(API).get_json()

        assert created["token"]["scopes"] == ["project:read"]
        assert listing[0]["scopes"] == ["project:read"]
        assert secret not in json.dumps(listing)


class TestRotation:
    def test_rotate_returns_a_new_secret_once_and_invalidates_the_old(
        self, client, make_user, login
    ):
        user = make_user()
        login()
        created = _create(client, label="rotating", scopes=["read"]).get_json()
        old_secret = created["secret"]

        response = client.post(f"{API}/{created['token']['id']}/rotate")
        body = response.get_json()

        assert response.status_code == 200
        assert body["secret"] != old_secret
        assert body["token"]["id"] == created["token"]["id"]
        assert api_tokens.authenticate(old_secret) is None
        assert api_tokens.authenticate(body["secret"]).user_id == user.id
        assert body["secret"] not in json.dumps(client.get(API).get_json())

    def test_rotate_another_users_token_is_not_found(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        row, _ = api_tokens.create_token(owner, scopes=["read"])
        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")

        response = client.post(f"{API}/{row.id}/rotate")

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"


class TestRevocation:
    def test_revocation_keeps_the_row_and_audit_history(self, client, make_user, login):
        user = make_user()
        login()
        created = _create(client, label="to revoke", scopes=["read"]).get_json()
        token_id = created["token"]["id"]

        response = client.delete(f"{API}/{token_id}")

        assert response.status_code == 200
        row = db.session.get(ApiToken, token_id)
        assert row.revoked_at is not None
        assert row.user_id == user.id
        assert any(item["revoked_at"] for item in client.get(API).get_json())
        actions = [
            entry.action
            for entry in AuditLog.query.filter_by(target_type="api_token", target_id=token_id)
            .order_by(AuditLog.id)
            .all()
        ]
        assert actions == [
            audit.API_TOKEN_CREATED,
            audit.API_TOKEN_REVOKED,
        ]
        assert "secret" not in (AuditLog.query.first().meta or "")

    def test_second_revocation_is_a_conflict(self, client, make_user, login):
        make_user()
        login()
        token_id = _create(client, scopes=["read"]).get_json()["token"]["id"]

        assert client.delete(f"{API}/{token_id}").status_code == 200
        second = client.delete(f"{API}/{token_id}")

        assert second.status_code == 409
        assert second.get_json()["code"] == "conflict"

    def test_rotate_after_revocation_is_a_conflict(self, client, make_user, login):
        make_user()
        login()
        token_id = _create(client, scopes=["read"]).get_json()["token"]["id"]
        client.delete(f"{API}/{token_id}")

        response = client.post(f"{API}/{token_id}/rotate")

        assert response.status_code == 409
        assert response.get_json()["code"] == "conflict"

    def test_revoking_another_users_token_is_not_found(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        row, _ = api_tokens.create_token(owner, scopes=["read"])
        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")

        response = client.delete(f"{API}/{row.id}")

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"


class TestAuditTrail:
    def test_create_and_rotate_are_audited_without_secrets(self, client, make_user, login):
        make_user()
        login()
        created = _create(client, label="audited", scopes=["read"]).get_json()
        rotated = client.post(f"{API}/{created['token']['id']}/rotate").get_json()

        entries = AuditLog.query.filter_by(target_type="api_token").order_by(AuditLog.id).all()
        assert [entry.action for entry in entries] == [
            audit.API_TOKEN_CREATED,
            audit.API_TOKEN_ROTATED,
        ]
        dumped = json.dumps([entry.metadata_dict for entry in entries])
        assert created["secret"] not in dumped
        assert rotated["secret"] not in dumped
