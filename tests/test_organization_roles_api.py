"""Organization role CRUD API tests (#598)."""

from __future__ import annotations

import json
from pathlib import Path

from app.extensions import db
from app.models import AuditLog, OrganizationRole, User, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE
from app.services import audit, permissions

REPO_ROOT = Path(__file__).resolve().parents[1]


def _user(username: str, email: str):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner, name: str):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _member(workspace, user, role: str = "viewer"):
    member = WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role=role, status=STATUS_ACTIVE
    )
    db.session.add(member)
    db.session.commit()
    return member


def _builtin(owner, name: str = "admin"):
    role = OrganizationRole(
        owner_id=owner.id,
        name=name,
        description=f"Built-in {name} role",
        is_builtin=True,
    )
    role.capabilities = []
    db.session.add(role)
    db.session.commit()
    return role


def _audit_rows(workspace_id: int):
    return AuditLog.query.filter_by(workspace_id=workspace_id).order_by(AuditLog.id).all()


class TestOwnerCrud:
    def test_full_crud_lifecycle_is_audited(self, client, login, app):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner, "Acme")
        login(email="owner@example.com")

        created = client.post(
            f"/api/workspaces/{workspace.id}/roles",
            json={"name": "Auditor", "capabilities": ["view_members", "comment"]},
        )
        assert created.status_code == 201
        role_id = created.get_json()["id"]
        assert created.get_json()["capabilities"] == ["comment", "view_members"]
        assert created.get_json()["is_builtin"] is False

        listed = client.get(f"/api/workspaces/{workspace.id}/roles")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.get_json()["roles"]] == [role_id]

        detail = client.get(f"/api/workspaces/{workspace.id}/roles/{role_id}")
        assert detail.status_code == 200
        assert detail.get_json()["name"] == "Auditor"

        updated = client.put(
            f"/api/workspaces/{workspace.id}/roles/{role_id}",
            json={"description": "Read-only review", "capabilities": ["view_members"]},
        )
        assert updated.status_code == 200
        assert updated.get_json()["description"] == "Read-only review"
        assert updated.get_json()["capabilities"] == ["view_members"]

        deleted = client.delete(f"/api/workspaces/{workspace.id}/roles/{role_id}")
        assert deleted.status_code == 200
        assert deleted.get_json() == {"deleted": True, "id": role_id}
        assert OrganizationRole.query.filter_by(id=role_id).first() is None

        rows = _audit_rows(workspace.id)
        assert [row.action for row in rows] == [
            audit.ORG_ROLE_CREATED,
            audit.ORG_ROLE_UPDATED,
            audit.ORG_ROLE_DELETED,
        ]
        assert all(row.user_id == owner.id for row in rows)
        payloads = [json.loads(row.meta) for row in rows]
        assert payloads[0]["actor_id"] == owner.id
        assert payloads[0]["before"] is None
        assert payloads[0]["after"]["name"] == "Auditor"
        assert payloads[1]["before"]["capabilities"] == ["comment", "view_members"]
        assert payloads[1]["after"]["capabilities"] == ["view_members"]
        assert payloads[2]["before"]["name"] == "Auditor"
        assert payloads[2]["after"] is None

    def test_duplicate_name_returns_conflict(self, client, login, app):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner, "Acme")
        login(email="owner@example.com")
        payload = {"name": "Auditor", "capabilities": []}
        assert client.post(f"/api/workspaces/{workspace.id}/roles", json=payload).status_code == 201

        response = client.post(f"/api/workspaces/{workspace.id}/roles", json=payload)

        assert response.status_code == 409
        assert response.get_json()["code"] == "conflict"
        assert response.get_json()["details"]["reason"] == "role_name_exists"

    def test_unknown_capability_is_structured_invalid_input(self, client, login, app):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner, "Acme")
        login(email="owner@example.com")

        response = client.post(
            f"/api/workspaces/{workspace.id}/roles",
            json={"name": "Auditor", "capabilities": ["project:read"]},
        )

        assert response.status_code == 400
        payload = response.get_json()
        assert payload["code"] == "invalid_input"
        assert payload["details"]["capability"] == "project:read"
        assert payload["details"]["reason"] == "invalid_input"

    def test_builtin_roles_are_immutable(self, client, login, app):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner, "Acme")
        role = _builtin(owner)
        login(email="owner@example.com")

        updated = client.put(
            f"/api/workspaces/{workspace.id}/roles/{role.id}",
            json={"name": "Superadmin"},
        )
        deleted = client.delete(f"/api/workspaces/{workspace.id}/roles/{role.id}")

        assert updated.status_code == 409
        assert updated.get_json()["details"]["reason"] == "builtin_role_protected"
        assert deleted.status_code == 409
        assert OrganizationRole.query.filter_by(id=role.id).first() is not None


class TestAuthorization:
    def test_anonymous_request_is_structured_401(self, client, app):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner, "Acme")

        response = client.get(f"/api/workspaces/{workspace.id}/roles")

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"

    def test_regular_member_is_forbidden(self, client, login, app):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner, "Acme")
        member = _user("member", "member@example.com")
        _member(workspace, member)
        login(email="member@example.com")

        response = client.get(f"/api/workspaces/{workspace.id}/roles")

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_another_organizations_role_is_not_found(self, client, login, app):
        owner = _user("owner", "owner@example.com")
        other = _user("other", "other@example.com")
        workspace = _workspace(owner, "Acme")
        other_role = _builtin(other, name="auditor")
        login(email="owner@example.com")

        response = client.get(f"/api/workspaces/{workspace.id}/roles/{other_role.id}")

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"

    def test_builtin_admin_capabilities_are_unchanged(self):
        assert permissions.role_can("admin", "manage_members") is False
        assert permissions.capabilities_for_role("admin") == []


class TestDocumentation:
    def test_openapi_lists_all_role_routes(self):
        spec = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        collection = "/api/workspaces/{workspace_id}/roles"
        detail = f"{collection}/{{role_id}}"

        assert set(spec["paths"][collection]) == {"get", "post"}
        assert set(spec["paths"][detail]) == {"get", "put", "delete"}
