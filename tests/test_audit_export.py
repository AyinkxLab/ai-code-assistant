"""Audit log export tests (#607)."""

from __future__ import annotations

import csv
import io
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


def _entry(user, action, *, days_ago=0, metadata=None, target_type="user", target_id=None):
    entry = AuditLog(
        user_id=user.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        meta=None if metadata is None else json.dumps(metadata),
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.session.add(entry)
    db.session.commit()
    return entry


class TestAuditExportAccess:
    def test_requires_login(self, client):
        assert client.get("/admin/audit/export").status_code == 302

    def test_non_admin_gets_structured_forbidden(self, client, make_user, login):
        make_user(username="member", email="member@example.com")
        login(email="member@example.com")

        response = client.get("/admin/audit/export")

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"


class TestAuditExportFilters:
    def test_actor_action_date_and_resource_filters(self, client, login):
        admin = _user("admin", "admin@example.com", admin=True)
        worker = _user("worker", "worker@example.com")
        _entry(worker, audit.USER_CREATED, days_ago=0, target_type="user", target_id=7)
        _entry(admin, audit.LOGIN_SUCCESS, days_ago=0, target_type="session")
        _entry(worker, audit.PROVIDER_BILLING_IMPORTED, days_ago=10)
        login(email="admin@example.com")

        by_actor = client.get("/admin/audit/export?actor=worker")
        rows = json.loads(by_actor.data)
        assert {row["action"] for row in rows} >= {
            audit.USER_CREATED,
            audit.PROVIDER_BILLING_IMPORTED,
        }
        assert audit.LOGIN_SUCCESS not in {row["action"] for row in rows}

        by_action = json.loads(client.get("/admin/audit/export?action=login_success").data)
        assert {row["action"] for row in by_action} == {audit.LOGIN_SUCCESS}
        assert len(by_action) >= 2  # the seeded row plus the admin's login

        recent = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        recent_rows = json.loads(client.get(f"/admin/audit/export?start={recent}").data)
        assert audit.PROVIDER_BILLING_IMPORTED not in {row["action"] for row in recent_rows}

        by_resource = json.loads(
            client.get("/admin/audit/export?resource_type=user&resource_id=7").data
        )
        assert [row["target_id"] for row in by_resource] == [7]

    def test_unknown_actor_exports_nothing(self, client, login):
        _user("admin", "admin@example.com", admin=True)
        login(email="admin@example.com")

        response = client.get("/admin/audit/export?actor=ghost")

        assert response.status_code == 200
        assert json.loads(response.data) == []


class TestAuditExportSafety:
    def test_json_excludes_metadata_username_secrets_and_prompt_content(self, client, login):
        admin = _user("admin", "admin@example.com", admin=True)
        _entry(
            admin,
            audit.LOGIN_FAILURE,
            metadata={
                "access_token": "super-secret-token",
                "prompt": "private prompt content",
                "email": "worker@example.com",
            },
        )
        login(email="admin@example.com")

        body = client.get("/admin/audit/export?action=login_failure").get_data(as_text=True)
        rows = json.loads(body)

        assert len(rows) == 1
        assert set(rows[0]) == {
            "id",
            "workspace_id",
            "user_id",
            "action",
            "target_type",
            "target_id",
            "created_at",
            "prev_hash",
            "record_hash",
        }
        for forbidden in (
            "super-secret-token",
            "private prompt content",
            "worker@example.com",
            "admin",
            "metadata",
        ):
            assert forbidden not in body

    def test_csv_has_stable_header_and_rows(self, client, login):
        admin = _user("admin", "admin@example.com", admin=True)
        _entry(admin, audit.USER_CREATED, metadata={"prompt": "do not export"})
        login(email="admin@example.com")

        response = client.get("/admin/audit/export?format=csv&action=user_created")

        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        assert "do not export" not in response.get_data(as_text=True)
        reader = csv.DictReader(io.StringIO(response.get_data(as_text=True)))
        rows = list(reader)
        assert reader.fieldnames == [
            "id",
            "workspace_id",
            "user_id",
            "action",
            "target_type",
            "target_id",
            "created_at",
            "prev_hash",
            "record_hash",
        ]
        assert rows[0]["action"] == audit.USER_CREATED


class TestAuditExportErrorsAndBounds:
    def test_invalid_format_is_structured(self, client, login):
        _user("admin", "admin@example.com", admin=True)
        login(email="admin@example.com")

        response = client.get("/admin/audit/export?format=xml")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_invalid_dates_and_limit_are_structured(self, client, login):
        _user("admin", "admin@example.com", admin=True)
        login(email="admin@example.com")

        bad_date = client.get("/admin/audit/export?start=not-a-date")
        assert bad_date.status_code == 400
        assert bad_date.get_json()["code"] == "invalid_input"

        reversed_dates = client.get("/admin/audit/export?start=2026-02-01&end=2026-01-01")
        assert reversed_dates.status_code == 400

        bad_limit = client.get("/admin/audit/export?limit=0")
        assert bad_limit.status_code == 400
        assert bad_limit.get_json()["code"] == "invalid_input"

    def test_limit_caps_streamed_rows(self, client, login):
        admin = _user("admin", "admin@example.com", admin=True)
        for _ in range(5):
            _entry(admin, audit.USER_CREATED)
        login(email="admin@example.com")

        response = client.get("/admin/audit/export?action=user_created&limit=2")

        assert response.headers["X-Export-Limit"] == "2"
        assert response.headers["Cache-Control"] == "no-store"
        assert len(json.loads(response.data)) == 2
