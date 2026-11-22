"""User data export workflow tests (#614)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import AuditLog, Conversation, Message, Prompt, UserDataExport
from app.models.user_data_export import (
    STATUS_DOWNLOADED,
    STATUS_EXPIRED,
    STATUS_READY,
    STATUS_REQUESTED,
)
from app.services import audit, data_export


def _user(make_user, username="tester", email="tester@example.com"):
    return make_user(username=username, email=email)


class TestRequestWorkflow:
    def test_request_creates_ready_export_with_one_time_token(self, client, make_user, login):
        user = _user(make_user)
        login()

        response = client.post("/auth/api/data-export")

        assert response.status_code == 201
        body = response.get_json()
        assert body["status"] == STATUS_READY
        assert body["downloadable"] is True
        assert body["download_token"]
        assert body["download_path"] == f"/auth/api/data-export/{body['id']}/download"

        row = db.session.get(UserDataExport, body["id"])
        assert row.user_id == user.id
        assert row.token_hash
        assert body["download_token"] not in row.token_hash

        actions = {entry.action for entry in AuditLog.query.filter_by(target_id=row.id).all()}
        assert actions >= {audit.DATA_EXPORT_REQUESTED, audit.DATA_EXPORT_READY}

    def test_service_transitions_requested_processing_ready_downloaded(self, app, make_user):
        user = _user(make_user)
        now = datetime(2026, 1, 1, tzinfo=UTC)

        row = data_export.request_export(user, now=now, enforce_rate_limit=False)
        assert row.status == STATUS_REQUESTED

        row, token = data_export.process_request(row.id, now=now)
        assert row.status == STATUS_READY
        assert row.processing_started_at is not None
        assert row.completed_at is not None
        assert row.expires_at.replace(tzinfo=UTC) == now + data_export.DOWNLOAD_TTL

        payload = data_export.download_export(row.id, token, user, now=now)
        assert payload["user"]["id"] == user.id
        assert payload["kind"] == "user-data-export"
        assert row.status == STATUS_DOWNLOADED
        assert row.downloaded_at is not None

        with pytest.raises(data_export.ExportAlreadyDownloaded):
            data_export.download_export(row.id, token, user, now=now)

    def test_export_payload_contains_own_content_but_no_secret_material(self, app, make_user):
        user = _user(make_user)
        conversation = Conversation(user_id=user.id, title="My conversation")
        db.session.add(conversation)
        db.session.flush()
        db.session.add(Message(conversation_id=conversation.id, role="user", content="hello"))
        db.session.add(Prompt(user_id=user.id, title="P", content="prompt body"))
        db.session.commit()

        payload = data_export.build_export_payload(user)

        assert payload["user"]["email"] == user.email
        assert payload["conversations"][0]["messages"][0]["content"] == "hello"
        assert payload["prompts"][0]["content"] == "prompt body"
        assert "password_hash" not in str(payload)


class TestExpiryAndRateLimit:
    def test_expired_link_is_gone_and_cannot_be_reused(self, app, make_user):
        user = _user(make_user)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        row = data_export.request_export(user, now=now, enforce_rate_limit=False)
        row, token = data_export.process_request(row.id, now=now)

        with pytest.raises(data_export.ExportExpired):
            data_export.download_export(
                row.id, token, user, now=now + data_export.DOWNLOAD_TTL + timedelta(seconds=1)
            )

        assert row.status == STATUS_EXPIRED
        with pytest.raises(data_export.ExportExpired):
            data_export.download_export(row.id, token, user, now=now)

    def test_requests_are_rate_limited(self, client, make_user, login, monkeypatch):
        _user(make_user)
        login()
        monkeypatch.setattr(data_export, "REQUEST_LIMIT", 1)

        assert client.post("/auth/api/data-export").status_code == 201
        limited = client.post("/auth/api/data-export")

        assert limited.status_code == 429
        assert limited.get_json()["code"] == "rate_limited"
        assert limited.headers["Retry-After"]

    def test_expired_download_route_returns_gone(self, client, make_user, login):
        _user(make_user)
        login()
        created = client.post("/auth/api/data-export").get_json()
        row = db.session.get(UserDataExport, created["id"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.session.commit()

        response = client.get(
            f"/auth/api/data-export/{row.id}/download?token={created['download_token']}"
        )

        assert response.status_code == 410
        assert response.get_json()["code"] == "gone"
        assert db.session.get(UserDataExport, row.id).status == STATUS_EXPIRED


class TestDownloadAndAuthorization:
    def test_download_returns_account_data_then_rejects_reuse(self, client, make_user, login):
        _user(make_user)
        login()
        created = client.post("/auth/api/data-export").get_json()

        response = client.get(
            f"/auth/api/data-export/{created['id']}/download" f"?token={created['download_token']}"
        )

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "attachment" in response.headers["Content-Disposition"]
        assert response.get_json()["user"]["username"] == "tester"

        reused = client.get(
            f"/auth/api/data-export/{created['id']}/download" f"?token={created['download_token']}"
        )
        assert reused.status_code == 409
        assert reused.get_json()["code"] == "conflict"

    def test_other_user_cannot_read_or_download(self, client, make_user, login):
        owner = _user(make_user)
        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        row = data_export.request_export(owner, enforce_rate_limit=False)
        row, token = data_export.process_request(row.id)

        assert client.get(f"/auth/api/data-export/{row.id}").status_code == 404
        download = client.get(f"/auth/api/data-export/{row.id}/download?token={token}")
        assert download.status_code == 404
        assert download.get_json()["code"] == "not_found"

    def test_missing_and_wrong_tokens_are_structured(self, client, make_user, login):
        _user(make_user)
        login()
        created = client.post("/auth/api/data-export").get_json()
        base = f"/auth/api/data-export/{created['id']}/download"

        missing = client.get(base)
        assert missing.status_code == 400
        assert missing.get_json()["code"] == "invalid_input"

        wrong = client.get(f"{base}?token=not-the-token")
        assert wrong.status_code == 404
        assert wrong.get_json()["code"] == "not_found"

    def test_list_is_scoped_to_the_current_user(self, client, make_user, login):
        owner = _user(make_user)
        make_user(username="other", email="other@example.com")
        data_export.request_export(owner, enforce_rate_limit=False)
        login(email="other@example.com")

        response = client.get("/auth/api/data-export")

        assert response.status_code == 200
        assert response.get_json()["items"] == []


def test_request_requires_login(client):
    assert client.post("/auth/api/data-export").status_code == 302
    assert client.get("/auth/api/data-export").status_code == 302
