"""Tests for encrypted LLM provider API keys (#1): AES-GCM round-trip, tamper
detection, redaction, owner-scoped CRUD, and no-plaintext-at-rest."""

import base64
import json

import pytest

from app.extensions import db
from app.models import ApiKey
from app.services import api_keys as api_keys_service
from app.services.crypto import (
    decrypt_api_key,
    encrypt_api_key,
    mask_api_key,
)


class TestApiKeyCrypto:
    def test_round_trip(self, app):
        token = encrypt_api_key("sk-test-1234567890")
        assert token != "sk-test-1234567890"
        assert decrypt_api_key(token) == "sk-test-1234567890"

    def test_random_nonce_per_record(self, app):
        first = encrypt_api_key("same-secret")
        second = encrypt_api_key("same-secret")
        assert first != second
        assert decrypt_api_key(first) == decrypt_api_key(second) == "same-secret"

    def test_tamper_detection(self, app):
        token = encrypt_api_key("sk-tamper-me")
        raw = bytearray(base64.urlsafe_b64decode(token.encode("ascii")))
        raw[-1] ^= 0x01
        tampered = base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
        with pytest.raises(ValueError):
            decrypt_api_key(tampered)

    def test_rotated_secret_cannot_decrypt(self, app):
        token = encrypt_api_key("sk-rotate")
        app.config["SECRET_KEY"] = "a-completely-different-secret"
        with pytest.raises(ValueError):
            decrypt_api_key(token)

    def test_malformed_token_rejected(self, app):
        with pytest.raises(ValueError):
            decrypt_api_key("not-base64!!!")

    def test_mask_api_key(self):
        assert mask_api_key("sk-1234567890") == "sk-1…7890"
        assert mask_api_key("short") == "*****"
        assert mask_api_key("") == ""


class TestApiKeyService:
    def test_create_stores_ciphertext_only(self, app, make_user):
        user = make_user()
        key = api_keys_service.create_key(
            user, provider="openai", secret="sk-live-abcdefghij", label="prod"
        )
        assert key.encrypted_value != "sk-live-abcdefghij"
        assert "sk-live-abcdefghij" not in key.encrypted_value
        assert api_keys_service.decrypt_for_use(key) == "sk-live-abcdefghij"

    def test_to_dict_is_redacted(self, app, make_user):
        user = make_user()
        key = api_keys_service.create_key(user, provider="anthropic", secret="sk-ant-secret")
        payload = key.to_dict()
        dumped = json.dumps(payload)
        assert "sk-ant-secret" not in dumped
        assert "encrypted_value" not in dumped
        assert payload["provider"] == "anthropic"
        assert payload["has_key"] is True

    def test_db_row_has_no_plaintext(self, app, make_user):
        user = make_user()
        key = api_keys_service.create_key(user, provider="openai", secret="sk-plain-marker")
        dumped = str({col.name: getattr(key, col.name) for col in key.__table__.columns})
        assert "sk-plain-marker" not in dumped

    def test_create_requires_secret_and_provider(self, app, make_user):
        user = make_user()
        with pytest.raises(api_keys_service.ApiKeyError):
            api_keys_service.create_key(user, provider="openai", secret="   ")
        with pytest.raises(api_keys_service.ApiKeyError):
            api_keys_service.create_key(user, provider="  ", secret="sk-x")

    def test_get_owned_is_scoped(self, app, make_user):
        owner = make_user()
        intruder = make_user(username="intruder", email="intruder@example.com")
        key = api_keys_service.create_key(owner, provider="openai", secret="sk-owned")
        assert api_keys_service.get_owned(owner, key.id) is not None
        assert api_keys_service.get_owned(intruder, key.id) is None


class TestApiKeyRoutes:
    def test_page_requires_login(self, client):
        assert client.get("/keys/").status_code == 302

    def test_page_renders(self, client, make_user, login):
        make_user()
        login()
        assert client.get("/keys/").status_code == 200

    def test_create_and_list_redacted(self, client, make_user, login):
        user = make_user()
        login()
        response = client.post(
            "/keys/api/keys",
            json={"provider": "openai", "key": "sk-live-1234567890", "label": "prod"},
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["provider"] == "openai"
        assert "sk-live-1234567890" not in json.dumps(body)

        listing = client.get("/keys/api/keys").get_json()
        assert len(listing) == 1
        assert "sk-live-1234567890" not in json.dumps(listing)

        row = ApiKey.query.filter_by(user_id=user.id).first()
        assert api_keys_service.decrypt_for_use(row) == "sk-live-1234567890"

    def test_create_requires_key(self, client, make_user, login):
        make_user()
        login()
        response = client.post("/keys/api/keys", json={"provider": "openai", "key": "  "})
        assert response.status_code == 400

    def test_delete_own_key(self, client, make_user, login):
        user = make_user()
        login()
        created = client.post(
            "/keys/api/keys", json={"provider": "openai", "key": "sk-delete-me"}
        ).get_json()
        response = client.delete(f"/keys/api/keys/{created['id']}")
        assert response.status_code == 200
        assert ApiKey.query.filter_by(user_id=user.id).count() == 0

    def test_update_active_flag(self, client, make_user, login):
        make_user()
        login()
        created = client.post(
            "/keys/api/keys", json={"provider": "openai", "key": "sk-toggle"}
        ).get_json()
        response = client.patch(f"/keys/api/keys/{created['id']}", json={"is_active": False})
        assert response.status_code == 200
        assert response.get_json()["is_active"] is False

    def test_owner_scoping_enforced(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        key = api_keys_service.create_key(owner, provider="openai", secret="sk-owner")
        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")

        assert client.get("/keys/api/keys").get_json() == []
        assert client.delete(f"/keys/api/keys/{key.id}").status_code == 404
        assert (
            client.patch(f"/keys/api/keys/{key.id}", json={"is_active": False}).status_code == 404
        )
        assert db.session.get(ApiKey, key.id) is not None
