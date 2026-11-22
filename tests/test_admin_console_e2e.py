"""End-to-end coverage for the admin console pages.

Deterministic Flask test-client checks for ``/admin/actions`` and
``/admin/usage/report``: authentication, admin authorization, the rendered
heading and empty state, and structured rejection of invalid filters.
"""

from __future__ import annotations

from app.extensions import db
from app.models import AuditLog


def _admin(make_user, username="admin", email="admin@example.com"):
    user = make_user(username=username, email=email)
    user.is_admin = True
    db.session.commit()
    return user


ADMIN_PAGES = (
    ("/admin/actions", b"Admin actions", b"No administrative actions match these filters."),
    (
        "/admin/usage/report",
        b"Admin usage report",
        b"No usage rows match this window and scope.",
    ),
)

INVALID_FILTERS = (
    "/admin/actions?start=not-a-date",
    "/admin/usage/report?scope=team",
)


class TestAdminConsoleAuthentication:
    def test_unauthenticated_is_redirected_to_login(self, client):
        for path, _, _ in ADMIN_PAGES:
            assert client.get(path).status_code == 302

    def test_non_admin_is_forbidden(self, client, make_user, login):
        make_user(username="member", email="member@example.com")
        login(email="member@example.com")

        for path, _, _ in ADMIN_PAGES:
            response = client.get(path)
            assert response.status_code == 403
            assert response.get_json()["code"] == "forbidden"


class TestAdminConsoleRendering:
    def test_admin_sees_heading_and_empty_state(self, client, make_user, login):
        _admin(make_user)
        login(email="admin@example.com")
        # The login records an audit entry, so clear action rows to assert the
        # documented empty state deterministically.
        AuditLog.query.delete()
        db.session.commit()

        for path, heading, empty_state in ADMIN_PAGES:
            response = client.get(path)
            assert response.status_code == 200
            assert heading in response.data
            assert empty_state in response.data

    def test_invalid_filters_are_rejected(self, client, make_user, login):
        _admin(make_user)
        login(email="admin@example.com")

        for path in INVALID_FILTERS:
            response = client.get(path)
            assert response.status_code == 400
            assert response.get_json()["code"] == "invalid_input"
