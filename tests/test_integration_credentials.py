"""Tests for encrypted integration credentials (#683).

Covers AES-GCM round trips, fresh ciphertext on every write, metadata-only
serialization, rotation, and the structured permission/input error paths.
"""

import json

import pytest

from app.extensions import db
from app.models import IntegrationCredential, Workspace
from app.services.crypto import decrypt_api_key
from app.services.integration_credentials import (
    IntegrationCredentialError,
    IntegrationCredentialNotFound,
    IntegrationCredentialPermissionError,
    decrypt_for_use,
    delete_credential,
    get_credential,
    list_credentials,
    rotate_credential,
    rotate_workspace_credentials,
    set_credential,
)


@pytest.fixture()
def workspace(make_user):
    owner = make_user(username="owner", email="owner@example.com")
    workspace = Workspace(user_id=owner.id, name="Integrations")
    db.session.add(workspace)
    db.session.commit()
    return workspace, owner


class TestRoundTrip:
    def test_encrypts_and_decrypts(self, workspace):
        ws, owner = workspace
        secret = "gho_plain_test_secret"

        credential = set_credential(ws.id, "github", "access_token", secret, user=owner)

        assert credential.encrypted_value != secret
        assert secret not in credential.encrypted_value
        assert decrypt_for_use(credential) == secret
        assert decrypt_api_key(credential.encrypted_value) == secret

    def test_ciphertext_differs_across_writes(self, workspace):
        ws, owner = workspace
        first = set_credential(ws.id, "github", "access_token", "same-secret", user=owner)
        first_ciphertext = first.encrypted_value

        second = set_credential(ws.id, "github", "access_token", "same-secret", user=owner)

        assert second.encrypted_value != first_ciphertext
        assert decrypt_for_use(second) == "same-secret"
        assert IntegrationCredential.query.filter_by(workspace_id=ws.id).count() == 1


class TestRedaction:
    def test_metadata_never_contains_the_secret_or_ciphertext(self, workspace):
        ws, owner = workspace
        secret = "super-secret-value"
        credential = set_credential(ws.id, "stellar", "signing_key", secret, user=owner)

        payload = json.dumps(credential.to_dict())

        assert secret not in payload
        assert credential.encrypted_value not in payload
        assert credential.to_dict()["has_secret"] is True
        assert secret not in repr(credential)
        assert credential.encrypted_value not in repr(credential)

    def test_list_returns_metadata_only(self, workspace):
        ws, owner = workspace
        set_credential(ws.id, "github", "access_token", "one", user=owner)
        set_credential(ws.id, "github", "refresh_token", "two", user=owner)

        payload = [credential.to_dict() for credential in list_credentials(ws.id, user=owner)]

        assert [entry["name"] for entry in payload] == ["access_token", "refresh_token"]
        assert all("encrypted_value" not in entry for entry in payload)


class TestRotation:
    def test_rotation_re_encrypts_and_preserves_the_secret(self, workspace):
        ws, owner = workspace
        credential = set_credential(ws.id, "github", "access_token", "rotate-me", user=owner)
        before = credential.encrypted_value

        rotate_credential(credential, user=owner)

        assert credential.encrypted_value != before
        assert decrypt_for_use(credential) == "rotate-me"
        assert credential.last_rotated_at is not None

    def test_workspace_rotation_covers_every_credential(self, workspace):
        ws, owner = workspace
        set_credential(ws.id, "github", "access_token", "a", user=owner)
        set_credential(ws.id, "github", "refresh_token", "b", user=owner)
        before = {
            credential.name: credential.encrypted_value
            for credential in list_credentials(ws.id, user=owner)
        }

        count = rotate_workspace_credentials(ws.id, user=owner)

        after = {
            credential.name: credential.encrypted_value
            for credential in list_credentials(ws.id, user=owner)
        }
        assert count == 2
        assert all(after[name] != before[name] for name in before)


class TestErrors:
    def test_non_owner_cannot_read_or_write(self, workspace, make_user):
        ws, owner = workspace
        set_credential(ws.id, "github", "access_token", "secret", user=owner)
        intruder = make_user(username="intruder", email="intruder@example.com")

        with pytest.raises(IntegrationCredentialPermissionError) as exc:
            list_credentials(ws.id, user=intruder)
        assert exc.value.code == "permission_denied"

        with pytest.raises(IntegrationCredentialPermissionError):
            set_credential(ws.id, "github", "access_token", "new", user=intruder)

    def test_invalid_input_is_structured(self, workspace):
        ws, owner = workspace
        with pytest.raises(IntegrationCredentialError) as exc:
            set_credential(ws.id, "", "access_token", "secret", user=owner)
        assert exc.value.code == "invalid_input"

        with pytest.raises(IntegrationCredentialError):
            set_credential(ws.id, "github", "access_token", "", user=owner)

    def test_missing_credential_is_structured(self, workspace):
        ws, owner = workspace
        with pytest.raises(IntegrationCredentialNotFound) as exc:
            get_credential(ws.id, "github", "missing", user=owner)
        assert exc.value.code == "not_found"

    def test_delete_removes_the_row(self, workspace):
        ws, owner = workspace
        set_credential(ws.id, "github", "access_token", "secret", user=owner)
        delete_credential(ws.id, "github", "access_token", user=owner)
        with pytest.raises(IntegrationCredentialNotFound):
            get_credential(ws.id, "github", "access_token", user=owner)
