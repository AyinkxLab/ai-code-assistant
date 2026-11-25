"""Scoped API token model, service, and authentication tests (#616)."""

from __future__ import annotations

import json

import pytest

from app.extensions import db
from app.models import ApiToken
from app.services import api_tokens


class TestTokenLifecycle:
    def test_secret_is_returned_once_and_never_stored(self, app, make_user):
        user = make_user()

        row, secret = api_tokens.create_token(user, label="ci", scopes=["read"])

        assert secret.startswith(api_tokens.SECRET_PREFIX)
        assert row.token_hash == api_tokens.hash_secret(secret)
        dumped = str({col.name: getattr(row, col.name) for col in row.__table__.columns})
        assert secret not in dumped
        assert secret not in json.dumps(row.to_dict())

    def test_create_validates_scopes(self, app, make_user):
        user = make_user()
        with pytest.raises(api_tokens.ApiTokenError):
            api_tokens.create_token(user, scopes=[])
        with pytest.raises(api_tokens.ApiTokenError):
            api_tokens.create_token(user, scopes=["superuser"])
        with pytest.raises(api_tokens.ApiTokenError):
            api_tokens.create_token(user, scopes=["project:admin"])

    def test_scope_normalization_is_canonical(self, app, make_user):
        user = make_user()
        row, _ = api_tokens.create_token(user, scopes="write,read")
        assert row.scopes == "read,write"
        assert row.scope_list == ["read", "write"]

    def test_capability_scopes_are_a_subset_of_owner_capabilities(self, app, make_user):
        user = make_user()
        row, _ = api_tokens.create_token(user, scopes=["project:write", "read", "project:read"])

        assert row.scopes == "read,project:read,project:write"
        assert row.scope_list == ["read", "project:read", "project:write"]
        assert row.capability_scopes == ["project:read", "project:write"]
        assert row.to_dict()["scopes"] == row.scope_list

        with pytest.raises(api_tokens.ApiTokenError):
            api_tokens.create_token(user, scopes=["superuser:read"])

    def test_rotate_invalidates_the_old_secret(self, app, make_user):
        user = make_user()
        row, old_secret = api_tokens.create_token(user, scopes=["read"])

        rotated, new_secret = api_tokens.rotate_token(user, row.id)

        assert rotated.id == row.id
        assert new_secret != old_secret
        assert api_tokens.authenticate(old_secret) is None
        assert api_tokens.authenticate(new_secret).id == row.id

    def test_revoked_tokens_cannot_authenticate_or_rotate(self, app, make_user):
        user = make_user()
        row, secret = api_tokens.create_token(user, scopes=["admin"])

        assert api_tokens.revoke_token(user, row.id) is not None
        assert api_tokens.authenticate(secret) is None
        with pytest.raises(api_tokens.ApiTokenError):
            api_tokens.rotate_token(user, row.id)

    def test_expired_tokens_are_inactive(self, app, make_user):
        from datetime import UTC, datetime, timedelta

        user = make_user()
        row, secret = api_tokens.create_token(
            user, scopes=["read"], expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )

        assert row.is_active is False
        assert api_tokens.authenticate(secret) is None

    def test_owner_scoping(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        row, _ = api_tokens.create_token(owner, scopes=["read"])

        assert api_tokens.get_owned(other, row.id) is None
        assert api_tokens.rotate_token(other, row.id) is None
        assert api_tokens.revoke_token(other, row.id) is None
        assert api_tokens.list_tokens(other) == []


class TestScopeHierarchy:
    def test_required_scope_by_method(self):
        assert api_tokens.required_scope("GET") == "read"
        assert api_tokens.required_scope("POST") == "write"
        assert api_tokens.required_scope("DELETE") == "admin"

    def test_ranked_grants(self, app, make_user):
        user = make_user()
        read_row, _ = api_tokens.create_token(user, scopes=["read"])
        write_row, _ = api_tokens.create_token(user, scopes=["write"])
        admin_row, _ = api_tokens.create_token(user, scopes=["admin"])

        assert api_tokens.token_allows(read_row, "read")
        assert not api_tokens.token_allows(read_row, "write")
        assert api_tokens.token_allows(write_row, "read")
        assert api_tokens.token_allows(write_row, "write")
        assert not api_tokens.token_allows(write_row, "admin")
        assert api_tokens.token_allows(admin_row, "admin")

    def test_capability_scopes_do_not_break_ranked_checks(self, app, make_user):
        user = make_user()
        row, _ = api_tokens.create_token(user, scopes=["project:read"])

        assert api_tokens.token_allows(row, "read") is False
        assert api_tokens.token_has_capability(row, "project:read") is True
        assert api_tokens.token_has_capability(row, "project:write") is False
        assert api_tokens.required_capability("/api/v1/projects", "GET") == "project:read"
        assert api_tokens.required_capability("/api/v1/projects", "POST") == "project:write"
        assert api_tokens.required_capability("/api/v1/unknown", "GET") is None


class TestBearerAuthentication:
    def test_read_token_can_call_a_read_endpoint(self, client, make_user):
        user = make_user()
        _, secret = api_tokens.create_token(user, scopes=["read"])

        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code == 200

    def test_read_token_is_denied_a_write(self, client, make_user):
        user = make_user()
        _, secret = api_tokens.create_token(user, scopes=["read"])

        response = client.post(
            "/workspaces/api/workspaces",
            json={"name": "Denied"},
            headers={"Authorization": f"Bearer {secret}"},
        )

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_write_token_can_create_a_workspace(self, client, make_user):
        user = make_user()
        _, secret = api_tokens.create_token(user, scopes=["write"])

        response = client.post(
            "/workspaces/api/workspaces",
            json={"name": "Scoped API"},
            headers={"Authorization": f"Bearer {secret}"},
        )

        assert response.status_code == 201
        assert response.get_json()["name"] == "Scoped API"

    def test_delete_requires_admin_scope(self, client, make_user):
        user = make_user()
        _, write_secret = api_tokens.create_token(user, scopes=["write"])
        _, admin_secret = api_tokens.create_token(user, scopes=["admin"])

        denied = client.delete(
            "/workspaces/api/workspaces/999999",
            headers={"Authorization": f"Bearer {write_secret}"},
        )
        allowed = client.delete(
            "/workspaces/api/workspaces/999999",
            headers={"Authorization": f"Bearer {admin_secret}"},
        )

        assert denied.status_code == 403
        assert allowed.status_code == 404
        assert allowed.get_json()["code"] == "not_found"

    def test_revoked_token_is_unauthorized(self, client, make_user):
        user = make_user()
        row, secret = api_tokens.create_token(user, scopes=["read"])
        api_tokens.revoke_token(user, row.id)

        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"

    def test_token_cannot_authenticate_html_routes(self, client, make_user):
        user = make_user()
        _, secret = api_tokens.create_token(user, scopes=["admin"])

        response = client.get("/workspaces", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"

    def test_authentication_records_last_used(self, client, make_user):
        user = make_user()
        row, secret = api_tokens.create_token(user, scopes=["read"])
        assert row.last_used_at is None

        client.get("/api/v1/projects", headers={"Authorization": f"Bearer {secret}"})

        refreshed = db.session.get(ApiToken, row.id)
        assert refreshed.last_used_at is not None

    def test_capability_token_can_call_its_mapped_read_endpoint(self, client, make_user):
        user = make_user()
        _, secret = api_tokens.create_token(user, scopes=["project:read"])

        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code == 200

    def test_over_scoped_capability_token_is_forbidden_with_details(self, client, make_user):
        user = make_user()
        _, secret = api_tokens.create_token(user, scopes=["project:write"])

        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code == 403
        body = response.get_json()
        assert body["code"] == "forbidden"
        assert body["details"]["required_capability"] == "project:read"
        assert body["details"]["granted_scopes"] == ["project:write"]
        assert secret not in response.get_data(as_text=True)

    def test_capability_token_on_unmapped_api_path_is_forbidden(self, client, make_user):
        user = make_user()
        _, secret = api_tokens.create_token(user, scopes=["project:read"])

        response = client.get("/api/v1/unknown", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code in (403, 404)
        if response.status_code == 403:
            assert response.get_json()["details"]["required_capability"] == "unmapped"

    def test_expired_capability_token_is_unauthorized(self, client, make_user):
        from datetime import UTC, datetime, timedelta

        user = make_user()
        _, secret = api_tokens.create_token(
            user,
            scopes=["project:read"],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"

    def test_revoked_capability_token_is_unauthorized(self, client, make_user):
        user = make_user()
        row, secret = api_tokens.create_token(user, scopes=["project:read"])
        api_tokens.revoke_token(user, row.id)

        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {secret}"})

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"
