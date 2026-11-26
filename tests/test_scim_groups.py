"""Minimal SCIM Groups and safe role mapping tests (#593)."""

from __future__ import annotations

import json

import pytest

from app.extensions import db
from app.models import (
    ApiToken,
    AuditLog,
    GroupRoleMapping,
    ScimGroup,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services import api_tokens, audit, scim_groups


def _workspace(owner) -> Workspace:
    workspace = Workspace(user_id=owner.id, name="SCIM Org", slug="scim-org")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _users(*names):
    users = []
    for name in names:
        user = User(username=name, email=f"{name}@example.com")
        user.set_password("supersecret123")
        db.session.add(user)
        users.append(user)
    db.session.commit()
    return users


def _token_headers(user, scope="admin"):
    row, secret = api_tokens.create_token(user, scopes=[scope])
    return row, {"Authorization": f"Bearer {secret}"}, secret


def _mapping(client, headers, workspace_id, role="contributor", external_id="engineering"):
    return client.post(
        f"/api/group-roles/{workspace_id}",
        headers=headers,
        json={"group_external_id": external_id, "role": role},
    )


def _group_payload(external_id="engineering", members=None):
    return {
        "schemas": [scim_groups.SCIM_GROUP_SCHEMA],
        "externalId": external_id,
        "displayName": "Engineering",
        "members": members or [],
    }


def _member(workspace_id, user_id) -> WorkspaceMember:
    return WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=user_id).one()


class TestRoleMappingSafety:
    def test_privileged_roles_are_rejected(self, client):
        owner = _users("owner")[0]
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)

        response = _mapping(client, headers, workspace.id, role="owner")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
        assert GroupRoleMapping.query.count() == 0

    def test_non_admin_member_cannot_create_a_mapping(self, db):
        owner, member = _users("owner", "member")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id, user_id=member.id, role="viewer", status="active"
            )
        )
        db.session.commit()

        with pytest.raises(scim_groups.ScimPermissionDenied):
            scim_groups.create_mapping(
                workspace.id,
                actor=member,
                group_external_id="engineering",
                role="contributor",
            )

    def test_duplicate_mapping_conflicts(self, client):
        owner = _users("owner")[0]
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)

        assert _mapping(client, headers, workspace.id).status_code == 201
        duplicate = _mapping(client, headers, workspace.id)

        assert duplicate.status_code == 409
        assert duplicate.get_json()["code"] == "conflict"


class TestGroupSynchronization:
    def test_create_applies_mapping_and_audits(self, client):
        owner, member = _users("owner", "member")
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)
        assert _mapping(client, headers, workspace.id).status_code == 201

        response = client.post(
            f"/scim/v2/Groups?workspace_id={workspace.id}",
            headers=headers,
            data=json.dumps(_group_payload(members=[{"email": member.email}])),
            content_type="application/scim+json",
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["externalId"] == "engineering"
        assert body["members"][0]["value"] == str(member.id)
        row = _member(workspace.id, member.id)
        assert row.role == "contributor"
        assert row.provisioning_source == scim_groups.PROVISIONING_SCIM
        actions = {entry.action for entry in AuditLog.query.all()}
        assert {audit.SCIM_ROLE_MAPPING_CREATED, audit.SCIM_GROUP_CREATED} <= actions

    def test_patch_add_and_remove_revokes_only_scim_memberships(self, client):
        owner, first, second, manual = _users("owner", "first", "second", "manual")
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)
        assert _mapping(client, headers, workspace.id, role="viewer").status_code == 201
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=manual.id,
                role="viewer",
                status="active",
            )
        )
        db.session.commit()
        group_id = client.post(
            f"/scim/v2/Groups?workspace_id={workspace.id}",
            headers=headers,
            json=_group_payload(members=[{"email": first.email}]),
        ).get_json()["id"]

        added = client.patch(
            f"/scim/v2/Groups/{group_id}",
            headers=headers,
            json={
                "Operations": [
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"email": second.email}],
                    }
                ]
            },
        )
        assert added.status_code == 200
        assert _member(workspace.id, second.id).role == "viewer"

        removed = client.patch(
            f"/scim/v2/Groups/{group_id}",
            headers=headers,
            json={
                "Operations": [
                    {
                        "op": "remove",
                        "path": "members",
                        "value": [{"email": first.email}],
                    }
                ]
            },
        )
        assert removed.status_code == 200
        assert _member(workspace.id, first.id).status == "removed"
        assert _member(workspace.id, second.id).status == "active"
        assert _member(workspace.id, manual.id).status == "active"

    def test_delete_group_revokes_its_roles(self, client):
        owner, member = _users("owner", "member")
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)
        assert _mapping(client, headers, workspace.id).status_code == 201
        group_id = client.post(
            f"/scim/v2/Groups?workspace_id={workspace.id}",
            headers=headers,
            json=_group_payload(members=[{"email": member.email}]),
        ).get_json()["id"]

        response = client.delete(f"/scim/v2/Groups/{group_id}", headers=headers)

        assert response.status_code == 204
        assert ScimGroup.query.count() == 0
        assert _member(workspace.id, member.id).status == "removed"

    def test_mapping_deletion_revokes_roles(self, client):
        owner, member = _users("owner", "member")
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)
        mapping_id = _mapping(client, headers, workspace.id).get_json()["id"]
        client.post(
            f"/scim/v2/Groups?workspace_id={workspace.id}",
            headers=headers,
            json=_group_payload(members=[{"email": member.email}]),
        )

        response = client.delete(f"/api/group-roles/{workspace.id}/{mapping_id}", headers=headers)

        assert response.status_code == 204
        assert _member(workspace.id, member.id).status == "removed"


class TestStructuredErrors:
    def test_unknown_member_is_invalid_input(self, client):
        owner = _users("owner")[0]
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)

        response = client.post(
            f"/scim/v2/Groups?workspace_id={workspace.id}",
            headers=headers,
            json=_group_payload(members=[{"email": "missing@example.com"}]),
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_missing_workspace_and_anonymous_access(self, client):
        owner = _users("owner")[0]
        workspace = _workspace(owner)
        _, headers, _ = _token_headers(owner)

        anonymous = client.post(
            f"/scim/v2/Groups?workspace_id={workspace.id}",
            json=_group_payload(),
        )
        missing = client.get("/scim/v2/Groups", headers=headers)

        assert anonymous.status_code == 401
        assert anonymous.get_json()["code"] == "unauthorized"
        assert missing.status_code == 400
        assert missing.get_json()["code"] == "invalid_input"

    def test_write_token_cannot_delete_and_secret_is_hashed(self, client):
        owner = _users("owner")[0]
        workspace = _workspace(owner)
        row, headers, secret = _token_headers(owner, scope="write")
        group_id = client.post(
            f"/scim/v2/Groups?workspace_id={workspace.id}",
            headers=headers,
            json=_group_payload(),
        ).get_json()["id"]

        denied = client.delete(f"/scim/v2/Groups/{group_id}", headers=headers)

        assert denied.status_code == 403
        assert denied.get_json()["code"] == "forbidden"
        stored = db.session.get(ApiToken, row.id)
        assert stored.token_hash != secret
        assert secret not in stored.token_hash
