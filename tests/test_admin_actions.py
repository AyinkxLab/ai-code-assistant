"""Admin action audit view tests (#632)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import AuditLog, User
from app.services import audit


def _user(username, email, *, admin=False):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    user.is_admin = admin
    db.session.add(user)
    db.session.commit()
    return user


def _entry(user, action, *, days_ago=0, metadata=None, target_id=None):
    entry = AuditLog(
        user_id=user.id,
        action=action,
        target_type="user",
        target_id=target_id,
        meta=None if metadata is None else json.dumps(metadata),
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.session.add(entry)
    db.session.commit()
    return entry


class TestAdminActionsAccess:
    def test_requires_login(self, client):
        assert client.get("/admin/actions").status_code == 302

    def test_non_admin_is_forbidden_with_catalog_code(self, client, make_user, login):
        make_user(username="member", email="member@example.com")
        login(email="member@example.com")

        response = client.get("/admin/actions")

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_admin_can_open_the_view(self, client, login):
        _user("admin", "admin@example.com", admin=True)
        login(email="admin@example.com")

        response = client.get("/admin/actions")

        assert response.status_code == 200
        assert b"Admin actions" in response.data


class TestAdminActionsFilters:
    def test_actor_action_and_date_filters(self, client, login):
        admin = _user("admin", "admin@example.com", admin=True)
        worker = _user("worker", "worker@example.com")
        _entry(worker, audit.USER_CREATED, days_ago=0)
        _entry(admin, audit.LOGIN_SUCCESS, days_ago=0)
        _entry(worker, audit.PROVIDER_BILLING_IMPORTED, days_ago=10)
        login(email="admin@example.com")

        by_actor = client.get("/admin/actions?actor=worker").data
        assert b"worker" in by_actor
        assert b"provider_billing_imported" in by_actor
        assert b"login_success" not in by_actor

        by_action = client.get("/admin/actions?action=login_success").data
        assert b"login_success" in by_action
        assert b"user_created" not in by_action

        cut_off = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        recent = client.get(f"/admin/actions?start={cut_off}").data
        assert b"login_success" in recent
        assert b"provider_billing_imported" not in recent

    def test_unknown_actor_returns_an_empty_page(self, client, login):
        _user("admin", "admin@example.com", admin=True)
        login(email="admin@example.com")
        AuditLog.query.delete()
        db.session.commit()

        response = client.get("/admin/actions?actor=ghost")

        assert response.status_code == 200
        assert b"No administrative actions match these filters." in response.data

    def test_invalid_dates_are_rejected(self, client, login):
        _user("admin", "admin@example.com", admin=True)
        login(email="admin@example.com")

        response = client.get("/admin/actions?start=not-a-date")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_start_after_end_is_rejected(self, client, login):
        _user("admin", "admin@example.com", admin=True)
        login(email="admin@example.com")

        response = client.get("/admin/actions?start=2026-02-01&end=2026-01-01")

        assert response.status_code == 400


class TestAdminActionsSafetyAndBounds:
    def test_metadata_and_secrets_are_not_rendered(self, client, login):
        admin = _user("admin", "admin@example.com", admin=True)
        _entry(admin, audit.LOGIN_FAILURE, metadata={"access_token": "super-secret-token"})
        login(email="admin@example.com")

        html = client.get("/admin/actions").get_data(as_text=True)

        assert "super-secret-token" not in html
        assert "metadata" not in html
        assert "target_type" not in html

        payload = client.get("/admin/actions?format=json").get_json()
        assert set(payload["items"][0]) == {
            "id",
            "created_at",
            "actor",
            "action",
            "target_type",
            "target_id",
        }
        assert "super-secret-token" not in str(payload)

    def test_per_page_is_capped_and_pagination_works(self, client, login):
        admin = _user("admin", "admin@example.com", admin=True)
        for _ in range(3):
            _entry(admin, audit.LOGIN_SUCCESS)
        login(email="admin@example.com")

        first = client.get("/admin/actions?per_page=1000")
        html = first.get_data(as_text=True)
        assert 'name="per_page" type="number" min="1" max="100" value="100"' in html
        assert "Page 1 of 1" in html

        second = client.get("/admin/actions?per_page=2&page=2")
        assert second.status_code == 200
        assert b"Page 2 of 2" in second.data
