"""Access review report tests (#610).

The report is admin-only, read-only, and lists every active workspace grant
(members plus the workspace owner) with the role, last activity, and a stale
flag. It is available as JSON or CSV.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_VIEWER


def _admin(make_user, username="admin", email="admin@example.com"):
    user = make_user(username=username, email=email)
    user.is_admin = True
    db.session.commit()
    return user


def _workspace(owner, name="Access workspace"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _member(workspace, user, role=ROLE_VIEWER, *, last_seen_at=None):
    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=role,
        last_seen_at=last_seen_at,
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def _rows_by_role(payload):
    return {row["role"]: row for row in payload["items"]}


class TestAccessReviewAccess:
    def test_requires_login(self, client):
        assert client.get("/admin/access-review").status_code == 302

    def test_non_admin_is_forbidden(self, client, make_user, login):
        make_user(username="member", email="member@example.com")
        login(email="member@example.com")

        response = client.get("/admin/access-review")

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_admin_sees_members_and_owners(self, client, make_user, login):
        admin = _admin(make_user)
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)

        now = datetime.now(UTC)
        owner.last_login_at = now - timedelta(days=100)
        member.last_seen_at = now - timedelta(days=1)
        member.last_login_at = now - timedelta(days=1)
        db.session.commit()
        _member(workspace, member)

        login(email=admin.email)
        response = client.get("/admin/access-review")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["truncated"] is False
        assert payload["total"] == 2

        rows = _rows_by_role(payload)
        assert set(rows) == {"owner", "viewer"}

        owner_row = rows["owner"]
        assert owner_row["workspace_id"] == workspace.id
        assert owner_row["workspace"] == workspace.name
        assert owner_row["user_id"] == owner.id
        assert owner_row["username"] == owner.username
        assert owner_row["email"] == owner.email
        assert owner_row["last_active_at"] is not None
        assert owner_row["stale"] is True

        member_row = rows["viewer"]
        assert member_row["user_id"] == member.id
        assert member_row["last_active_at"] is not None
        assert member_row["stale"] is False
        assert member_row["status"] == "active"


class TestAccessReviewFormats:
    def test_csv_has_attachment_disposition_and_truncation_header(self, client, make_user, login):
        admin = _admin(make_user)
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        _member(workspace, member)

        login(email=admin.email)
        response = client.get("/admin/access-review?format=csv")

        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        assert response.headers["Content-Disposition"] == (
            'attachment; filename="access-review.csv"'
        )
        assert response.headers["X-Export-Truncated"] == "0"

        body = response.get_data(as_text=True)
        header = body.splitlines()[0]
        assert header == (
            "workspace_id,workspace,user_id,username,email,role,last_active_at,stale,status"
        )
        assert "owner@example.com" in body
        assert "member@example.com" in body

    def test_invalid_format_is_rejected(self, client, make_user, login):
        admin = _admin(make_user)
        login(email=admin.email)

        response = client.get("/admin/access-review?format=xml")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
