"""SCIM 2.0 Users endpoint coverage (#592)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from app.models import ApiToken, User, UserSession, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, STATUS_ACTIVE
from app.services import api_tokens, scim_tokens

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

USER_PAYLOAD = {
    "schemas": [USER_SCHEMA],
    "userName": "alice",
    "displayName": "Alice",
    "emails": [{"value": "alice@example.com", "primary": True}],
    "active": True,
}


@pytest.fixture()
def scim_context(db):
    owner = User(username="owner", email="owner@example.com")
    owner.set_password("supersecret123")
    db.session.add(owner)
    db.session.commit()
    workspace = Workspace(user_id=owner.id, name="Acme")
    db.session.add(workspace)
    db.session.commit()
    _, secret = scim_tokens.create_token(owner.id, workspace.id)
    return {"owner": owner, "workspace": workspace, "secret": secret}


def _headers(secret):
    return {"Authorization": f"Bearer {secret}"}


def _assert_scim_error(response, status, scim_type=None):
    assert response.status_code == status
    body = response.get_json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert body["status"] == str(status)
    if scim_type is not None:
        assert body["scimType"] == scim_type


class TestAuthentication:
    def test_missing_token_is_rejected(self, client, scim_context):
        _assert_scim_error(client.get("/scim/v2/Users"), 401)

    def test_x_api_token_is_not_a_scim_credential(self, client, scim_context):
        response = client.get("/scim/v2/Users", headers={"X-API-Token": scim_context["secret"]})
        _assert_scim_error(response, 401)

    def test_personal_api_token_is_rejected(self, client, db, scim_context):
        row, secret = api_tokens.create_token(
            scim_context["owner"], scopes=["read", "write", "admin"]
        )
        assert isinstance(row, ApiToken)
        _assert_scim_error(client.get("/scim/v2/Users", headers=_headers(secret)), 401)

    def test_revoked_token_is_rejected(self, client, db, scim_context):
        token = scim_tokens.authenticate(scim_context["secret"])
        scim_tokens.revoke_token(scim_context["owner"].id, token.id)
        _assert_scim_error(
            client.get("/scim/v2/Users", headers=_headers(scim_context["secret"])), 401
        )

    def test_expired_token_is_rejected(self, client, db, scim_context):
        token = scim_tokens.authenticate(scim_context["secret"])
        token.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.session.commit()
        _assert_scim_error(
            client.get("/scim/v2/Users", headers=_headers(scim_context["secret"])), 401
        )

    def test_valid_token_never_creates_a_login_session(self, client, scim_context):
        response = client.get("/scim/v2/Users", headers=_headers(scim_context["secret"]))
        assert response.status_code == 200
        assert client.get("/auth/me").status_code == 302

    def test_personal_api_token_guard_ignores_scim_paths(self, app, client, scim_context):
        from flask import g

        @app.route("/scim/v2/_guard_probe")
        def _guard_probe():
            return {
                "api_token_identity": g.api_token is not None,
                "scim_token_identity": g.scim_token is not None,
            }

        response = client.get("/scim/v2/_guard_probe", headers=_headers(scim_context["secret"]))
        assert response.status_code == 200
        assert response.get_json() == {
            "api_token_identity": False,
            "scim_token_identity": True,
        }


class TestListAndGet:
    def test_list_includes_owner_and_provisioned_users(self, client, scim_context):
        created = client.post(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=USER_PAYLOAD
        )
        assert created.status_code == 201

        listed = client.get("/scim/v2/Users", headers=_headers(scim_context["secret"]))
        assert listed.status_code == 200
        body = listed.get_json()
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
        assert body["totalResults"] == 2
        assert {user["userName"] for user in body["Resources"]} == {"owner", "alice"}
        assert body["Resources"][0]["emails"][0]["value"] == "owner@example.com"

    @pytest.mark.parametrize(
        "filter_expression,expected",
        [
            ('userName eq "alice"', "alice"),
            ('emails.value eq "alice@example.com"', "alice"),
        ],
    )
    def test_supported_filters(self, client, scim_context, filter_expression, expected):
        client.post("/scim/v2/Users", headers=_headers(scim_context["secret"]), json=USER_PAYLOAD)
        response = client.get(
            "/scim/v2/Users",
            headers=_headers(scim_context["secret"]),
            query_string={"filter": filter_expression},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["userName"] == expected

    def test_unsupported_filter_returns_invalid_filter(self, client, scim_context):
        response = client.get(
            "/scim/v2/Users",
            headers=_headers(scim_context["secret"]),
            query_string={"filter": 'title eq "engineer"'},
        )
        _assert_scim_error(response, 400, "invalidFilter")

    @pytest.mark.parametrize(
        "query",
        [{"startIndex": "0"}, {"count": "0"}, {"count": "201"}, {"count": "many"}],
    )
    def test_invalid_pagination(self, client, scim_context, query):
        response = client.get(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), query_string=query
        )
        _assert_scim_error(response, 400, "invalidValue")

    def test_pagination_slices_results(self, client, scim_context):
        for index in range(2):
            payload = dict(USER_PAYLOAD)
            payload["userName"] = f"user{index}"
            payload["emails"] = [{"value": f"user{index}@example.com"}]
            assert (
                client.post(
                    "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=payload
                ).status_code
                == 201
            )
        response = client.get(
            "/scim/v2/Users",
            headers=_headers(scim_context["secret"]),
            query_string={"startIndex": "2", "count": "1"},
        )
        body = response.get_json()
        assert body["startIndex"] == 2
        assert body["itemsPerPage"] == 1
        assert body["totalResults"] == 3

    def test_get_one_and_missing_user(self, client, scim_context):
        created = client.post(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=USER_PAYLOAD
        ).get_json()
        response = client.get(
            f"/scim/v2/Users/{created['id']}", headers=_headers(scim_context["secret"])
        )
        assert response.status_code == 200
        assert response.get_json()["userName"] == "alice"
        _assert_scim_error(
            client.get("/scim/v2/Users/999999", headers=_headers(scim_context["secret"])), 404
        )


class TestCreate:
    def test_create_returns_201_with_usable_resource(self, client, db, scim_context):
        response = client.post(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=USER_PAYLOAD
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["schemas"] == [USER_SCHEMA]
        assert body["active"] is True
        assert response.headers["Location"] == f"/scim/v2/Users/{body['id']}"
        assert "password" not in body
        user = db.session.get(User, int(body["id"]))
        assert user.check_password(USER_PAYLOAD["userName"]) is False
        membership = WorkspaceMember.query.filter_by(
            workspace_id=scim_context["workspace"].id, user_id=user.id
        ).one()
        assert membership.role == ROLE_CONTRIBUTOR
        assert membership.status == STATUS_ACTIVE

    def test_duplicate_username_returns_uniqueness(self, client, scim_context):
        assert (
            client.post(
                "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=USER_PAYLOAD
            ).status_code
            == 201
        )
        duplicate = dict(USER_PAYLOAD)
        duplicate["emails"] = [{"value": "other@example.com"}]
        _assert_scim_error(
            client.post("/scim/v2/Users", headers=_headers(scim_context["secret"]), json=duplicate),
            409,
            "uniqueness",
        )

    def test_duplicate_email_returns_uniqueness(self, client, scim_context):
        assert (
            client.post(
                "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=USER_PAYLOAD
            ).status_code
            == 201
        )
        duplicate = dict(USER_PAYLOAD)
        duplicate["userName"] = "bob"
        _assert_scim_error(
            client.post("/scim/v2/Users", headers=_headers(scim_context["secret"]), json=duplicate),
            409,
            "uniqueness",
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"userName": "alice"},
            {**USER_PAYLOAD, "schemas": []},
            {**USER_PAYLOAD, "userName": ""},
            {**USER_PAYLOAD, "emails": [{"value": "not-an-email"}]},
        ],
    )
    def test_invalid_payload_is_structured(self, client, scim_context, payload):
        response = client.post(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=payload
        )
        assert response.status_code == 400
        assert response.get_json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]

    def test_non_json_body_is_invalid_syntax(self, client, scim_context):
        response = client.post(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), data="not-json"
        )
        _assert_scim_error(response, 400, "invalidSyntax")


class TestUpdateAndDeactivate:
    def _create(self, client, scim_context):
        response = client.post(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=USER_PAYLOAD
        )
        return response.get_json()

    def test_patch_replace_username_and_email(self, client, db, scim_context):
        resource = self._create(client, scim_context)
        response = client.patch(
            f"/scim/v2/Users/{resource['id']}",
            headers=_headers(scim_context["secret"]),
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {"op": "replace", "value": {"userName": "alice2"}},
                    {"op": "replace", "path": "emails", "value": [{"value": "a2@example.com"}]},
                ],
            },
        )
        assert response.status_code == 200
        assert response.get_json()["userName"] == "alice2"
        assert response.get_json()["emails"][0]["value"] == "a2@example.com"

    def test_patch_deactivation_revokes_sessions(self, client, db, scim_context):
        resource = self._create(client, scim_context)
        session_row = UserSession(id=secrets.token_hex(32), user_id=int(resource["id"]))
        db.session.add(session_row)
        db.session.commit()

        response = client.patch(
            f"/scim/v2/Users/{resource['id']}",
            headers=_headers(scim_context["secret"]),
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "replace", "value": {"active": False}}],
            },
        )
        assert response.status_code == 200
        assert response.get_json()["active"] is False
        user = db.session.get(User, int(resource["id"]))
        assert user.is_active is False
        assert db.session.get(UserSession, session_row.id).revoked_at is not None

    def test_delete_deactivates_user(self, client, db, scim_context):
        resource = self._create(client, scim_context)
        response = client.delete(
            f"/scim/v2/Users/{resource['id']}", headers=_headers(scim_context["secret"])
        )
        assert response.status_code == 204
        user = db.session.get(User, int(resource["id"]))
        assert user.is_active is False

    @pytest.mark.parametrize(
        "payload",
        [
            {"Operations": [{"op": "replace", "value": {"active": False}}]},
            {"schemas": [PATCH_SCHEMA], "Operations": []},
            {
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "remove", "value": {"active": False}}],
            },
            {
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "replace", "value": {"active": "false"}}],
            },
        ],
    )
    def test_invalid_patch_payload_is_structured(self, client, scim_context, payload):
        resource = self._create(client, scim_context)
        response = client.patch(
            f"/scim/v2/Users/{resource['id']}",
            headers=_headers(scim_context["secret"]),
            json=payload,
        )
        assert response.status_code == 400
        assert response.get_json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]

    def test_update_cannot_reuse_another_users_email(self, client, scim_context):
        first = self._create(client, scim_context)
        other = dict(USER_PAYLOAD)
        other["userName"] = "bob"
        other["emails"] = [{"value": "bob@example.com"}]
        second = client.post(
            "/scim/v2/Users", headers=_headers(scim_context["secret"]), json=other
        ).get_json()
        _assert_scim_error(
            client.patch(
                f"/scim/v2/Users/{second['id']}",
                headers=_headers(scim_context["secret"]),
                json={
                    "schemas": [PATCH_SCHEMA],
                    "Operations": [{"op": "replace", "value": {"emails": USER_PAYLOAD["emails"]}}],
                },
            ),
            409,
            "uniqueness",
        )
        assert first["userName"] == "alice"
