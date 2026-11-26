"""SCIM bearer token lifecycle and authentication (#594)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.extensions import db
from app.models import User, Workspace
from app.models.scim_token import ScimToken
from app.services import audit, scim_tokens

ROOT = Path(__file__).resolve().parents[1]


def _owner(username="owner", email="owner@example.com"):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner, name="Acme"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestTokenLifecycle:
    def test_secret_is_returned_once_and_never_stored(self, db):
        owner = _owner()
        workspace = _workspace(owner)
        row, secret = scim_tokens.create_token(
            owner.id, workspace.id, created_by_id=owner.id, label="Okta"
        )

        assert secret.startswith(scim_tokens.SECRET_PREFIX)
        assert row.token_hash == scim_tokens.hash_secret(secret)
        assert secret not in str({c.name: getattr(row, c.name) for c in row.__table__.columns})
        assert secret not in json.dumps(row.to_dict())

    def test_list_and_owner_scoping(self, db):
        owner = _owner()
        other = _owner("other", "other@example.com")
        workspace = _workspace(owner)
        other_workspace = _workspace(other, "Other")
        row, _ = scim_tokens.create_token(owner.id, workspace.id)
        scim_tokens.create_token(other.id, other_workspace.id)

        assert [token.id for token in scim_tokens.list_tokens(owner.id)] == [row.id]
        assert scim_tokens.get_owned(other.id, row.id) is None

    def test_rotate_replaces_secret_and_keeps_the_row(self, db):
        owner = _owner()
        workspace = _workspace(owner)
        row, old = scim_tokens.create_token(owner.id, workspace.id)
        rotated, new = scim_tokens.rotate_token(owner.id, row.id)

        assert rotated.id == row.id
        assert new != old
        assert scim_tokens.authenticate(old) is None
        assert scim_tokens.authenticate(new) is not None
        assert ScimToken.query.count() == 1

    def test_revoke_keeps_metadata_and_blocks_authentication(self, db):
        owner = _owner()
        workspace = _workspace(owner)
        row, secret = scim_tokens.create_token(owner.id, workspace.id)
        revoked = scim_tokens.revoke_token(owner.id, row.id, revoked_by_id=owner.id)

        assert revoked.id == row.id
        assert revoked.revoked_at is not None
        assert revoked.revoked_by_id == owner.id
        assert scim_tokens.authenticate(secret) is None

    def test_revoked_token_cannot_be_rotated(self, db):
        owner = _owner()
        workspace = _workspace(owner)
        row, _ = scim_tokens.create_token(owner.id, workspace.id)
        scim_tokens.revoke_token(owner.id, row.id)
        with pytest.raises(scim_tokens.ScimTokenError) as excinfo:
            scim_tokens.rotate_token(owner.id, row.id)
        assert excinfo.value.code == "conflict"

    def test_missing_unknown_expired_and_revoked_tokens_are_rejected(self, db):
        owner = _owner()
        workspace = _workspace(owner)
        _, live = scim_tokens.create_token(owner.id, workspace.id)
        expired, expired_secret = scim_tokens.create_token(
            owner.id,
            workspace.id,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        revoked, revoked_secret = scim_tokens.create_token(owner.id, workspace.id)
        scim_tokens.revoke_token(owner.id, revoked.id)

        for secret in ("", None, "sct_not-a-real-token", expired_secret, revoked_secret):
            assert scim_tokens.authenticate(secret) is None
        assert expired.is_active is False
        assert scim_tokens.authenticate(live) is not None

    def test_authenticate_records_last_use(self, db):
        owner = _owner()
        workspace = _workspace(owner)
        row, secret = scim_tokens.create_token(owner.id, workspace.id)
        assert row.last_used_at is None
        assert scim_tokens.authenticate(secret) is not None
        assert row.last_used_at is not None

    def test_invalid_label_is_structured(self, db):
        owner = _owner()
        workspace = _workspace(owner)
        with pytest.raises(scim_tokens.ScimTokenError) as excinfo:
            scim_tokens.create_token(owner.id, workspace.id, label="x" * 101)
        assert excinfo.value.code == "invalid_input"


class TestAdminRoutes:
    def test_page_requires_login(self, client, make_user):
        workspace = _workspace(make_user())
        assert client.get(f"/admin/organizations/{workspace.id}/scim-tokens").status_code == 302

    def test_issue_rotate_revoke_and_list(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        base = f"/admin/organizations/{workspace.id}/scim-tokens"

        issued = client.post(base, json={"label": "Okta"})
        assert issued.status_code == 201
        secret = issued.get_json()["secret"]
        assert secret.startswith(scim_tokens.SECRET_PREFIX)
        token_id = issued.get_json()["token"]["id"]
        assert "token_hash" not in issued.get_json()["token"]

        listed = client.get(base)
        assert listed.status_code == 200
        assert [row["id"] for row in listed.get_json()["tokens"]] == [token_id]
        assert secret not in listed.get_data(as_text=True)

        rotated = client.patch(f"{base}/{token_id}")
        assert rotated.status_code == 200
        new_secret = rotated.get_json()["secret"]
        assert new_secret != secret
        assert scim_tokens.authenticate(secret) is None
        assert scim_tokens.authenticate(new_secret) is not None

        revoked = client.delete(f"{base}/{token_id}")
        assert revoked.status_code == 200
        assert revoked.get_json()["token"]["is_active"] is False
        assert scim_tokens.authenticate(new_secret) is None

    def test_non_admin_is_forbidden(self, client, make_user, login, db):
        from app.models import WorkspaceMember
        from app.models.workspace_member import ROLE_VIEWER

        owner = make_user()
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()
        login(email="member@example.com")

        response = client.get(f"/admin/organizations/{workspace.id}/scim-tokens")
        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_invalid_input_not_found_and_conflict_are_structured(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        base = f"/admin/organizations/{workspace.id}/scim-tokens"

        invalid = client.post(base, json={"expires_at": "not-a-date"})
        assert invalid.status_code == 400
        assert invalid.get_json()["code"] == "invalid_input"

        missing = client.delete(f"{base}/999999")
        assert missing.status_code == 404
        assert missing.get_json()["code"] == "not_found"

        created = client.post(base).get_json()["token"]["id"]
        client.delete(f"{base}/{created}")
        conflict = client.patch(f"{base}/{created}")
        assert conflict.status_code == 409
        assert conflict.get_json()["code"] == "conflict"

    def test_audit_entries_are_appended(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        base = f"/admin/organizations/{workspace.id}/scim-tokens"
        token_id = client.post(base).get_json()["token"]["id"]
        client.patch(f"{base}/{token_id}")
        client.delete(f"{base}/{token_id}")

        actions = {row.action for row in audit.AuditLog.query.filter_by(workspace_id=workspace.id)}
        assert {
            audit.SCIM_TOKEN_ISSUED,
            audit.SCIM_TOKEN_ROTATED,
            audit.SCIM_TOKEN_REVOKED,
        } <= actions


class TestMigration:
    def test_scim_token_migration_chains_from_the_role_migration(self):
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations"))
        script = ScriptDirectory.from_config(config)

        assert script.get_heads() == ["c6d7e8f9a0b1"]
        assert script.get_revision("c6d7e8f9a0b1").down_revision == "c5d6e7f8a9b0"
