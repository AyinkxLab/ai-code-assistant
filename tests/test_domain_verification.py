"""Domain verification and SSO enforcement tests (#590)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Workspace, WorkspaceDomain
from app.services import domain_verification
from app.services.domain_verification import (
    ChallengeExpired,
    DomainConflict,
    DomainNotVerified,
    DomainPermissionDenied,
    InvalidDomain,
    VerificationFailed,
)


def _workspace(user_id, name="Acme"):
    workspace = Workspace(user_id=user_id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _verified_domain(workspace_id, domain="example.com", enforce=True):
    now = datetime.now(UTC)
    row = WorkspaceDomain(
        workspace_id=workspace_id,
        domain=domain,
        verification_token="token",
        challenge_expires_at=now + timedelta(hours=1),
        verified_at=now,
        enforce_sso=enforce,
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestNormalization:
    @pytest.mark.parametrize("value", ["example.com", "Sub.Example.COM.", "a-b.co.uk"])
    def test_accepts_valid_domains(self, value):
        assert domain_verification.normalize_domain(value) == value.rstrip(".").lower()

    @pytest.mark.parametrize(
        "value",
        ["", "example", "https://example.com", "example.com/path", "*.example.com", "a..com"],
    )
    def test_rejects_invalid_domains(self, value):
        with pytest.raises(InvalidDomain):
            domain_verification.normalize_domain(value)

    def test_txt_record_uses_the_domain(self):
        assert domain_verification.txt_record_name("Example.com") == (
            "_aica-verification.example.com"
        )
        assert domain_verification.expected_txt_value("abc") == "aica-verify=abc"


class TestChallengeAndVerification:
    def test_owner_creates_a_bounded_challenge(self, make_user):
        user = make_user()
        workspace = _workspace(user.id)
        now = datetime(2026, 1, 1, tzinfo=UTC)

        row = domain_verification.create_challenge(workspace.id, "Example.com", user=user, now=now)

        assert row.domain == "example.com"
        assert row.verification_token
        assert row.verified_at is None
        assert row.challenge_expires_at.replace(tzinfo=UTC) == (
            now + domain_verification.CHALLENGE_TTL
        )

    def test_non_owner_cannot_create_a_challenge(self, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        outsider = make_user(username="outsider", email="outsider@example.net")
        workspace = _workspace(owner.id)

        with pytest.raises(DomainPermissionDenied):
            domain_verification.create_challenge(workspace.id, "example.com", user=outsider)

    def test_verification_succeeds_with_matching_txt_record(self, make_user):
        user = make_user()
        workspace = _workspace(user.id)
        row = domain_verification.create_challenge(workspace.id, "example.com", user=user)
        expected = domain_verification.expected_txt_value(row.verification_token)

        verified = domain_verification.verify_domain(
            workspace.id,
            "example.com",
            user=user,
            resolver=lambda name: [expected],
        )

        assert verified.is_verified
        assert verified.verified_at is not None

    def test_verification_fails_without_the_txt_record(self, make_user):
        user = make_user()
        workspace = _workspace(user.id)
        domain_verification.create_challenge(workspace.id, "example.com", user=user)

        with pytest.raises(VerificationFailed):
            domain_verification.verify_domain(
                workspace.id,
                "example.com",
                user=user,
                resolver=lambda name: ["aica-verify=wrong"],
            )

        stored = WorkspaceDomain.query.filter_by(workspace_id=workspace.id).one()
        assert stored.verified_at is None

    def test_challenge_expiry_is_enforced(self, make_user):
        user = make_user()
        workspace = _workspace(user.id)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        row = domain_verification.create_challenge(workspace.id, "example.com", user=user, now=now)
        expected = domain_verification.expected_txt_value(row.verification_token)

        with pytest.raises(ChallengeExpired):
            domain_verification.verify_domain(
                workspace.id,
                "example.com",
                user=user,
                now=now + domain_verification.CHALLENGE_TTL + timedelta(seconds=1),
                resolver=lambda name: [expected],
            )

    def test_domain_cannot_be_claimed_by_two_workspaces(self, make_user):
        first = make_user(username="first", email="first@example.com")
        second = make_user(username="second", email="second@example.com")
        first_workspace = _workspace(first.id, "First")
        second_workspace = _workspace(second.id, "Second")

        domain_verification.create_challenge(first_workspace.id, "example.com", user=first)
        with pytest.raises(DomainConflict):
            domain_verification.create_challenge(second_workspace.id, "example.com", user=second)

    def test_enforcement_requires_a_verified_domain(self, make_user):
        user = make_user()
        workspace = _workspace(user.id)
        row = domain_verification.create_challenge(workspace.id, "example.com", user=user)

        with pytest.raises(DomainNotVerified):
            domain_verification.set_enforcement(workspace.id, row.id, True, user=user)


class TestDomainRoutes:
    def test_challenge_route_returns_record_details(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)

        response = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "Example.com"},
        )

        assert response.status_code == 201
        payload = response.get_json()
        assert payload["domain"] == "example.com"
        assert payload["txt_record_name"] == "_aica-verification.example.com"
        assert payload["txt_record_value"].startswith("aica-verify=")

    def test_verify_route_uses_dns(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)
        challenge = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "example.com"},
        ).get_json()

        with patch.object(
            domain_verification,
            "_resolve_txt",
            return_value=[challenge["txt_record_value"]],
        ):
            response = client.post(
                "/auth/api/domains/verify",
                json={"workspace_id": workspace.id, "domain": "example.com"},
            )

        assert response.status_code == 200
        assert response.get_json()["verified_at"] is not None

    def test_patch_enables_enforcement_after_verification(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user.id)
        row = _verified_domain(workspace.id, enforce=False)

        response = client.patch(
            f"/auth/api/domains/{row.id}",
            json={"workspace_id": workspace.id, "enforce_sso": True},
        )

        assert response.status_code == 200
        assert response.get_json()["enforce_sso"] is True

    def test_non_owner_gets_structured_permission_error(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="outsider", email="outsider@example.net")
        workspace = _workspace(owner.id)
        login(email="outsider@example.net")

        response = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "example.com"},
        )

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_invalid_input_gets_structured_error(self, client, make_user, login):
        make_user()
        login()

        response = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": "not-an-int", "domain": "example.com"},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_unauthenticated_call_gets_structured_401(self, client):
        response = client.post("/auth/api/domains/challenge", json={})

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"


class TestPasswordLoginBlock:
    def test_enforced_domain_blocks_password_login(self, client, make_user):
        user = make_user(username="blocked", email="user@example.com")
        workspace = _workspace(user.id)
        _verified_domain(workspace.id, domain="example.com", enforce=True)

        response = client.post(
            "/auth/login",
            data={"email": "user@example.com", "password": "supersecret123"},
        )

        assert response.status_code == 200
        assert b"Password sign-in is disabled" in response.data
        assert client.get("/auth/me").status_code == 302

    def test_other_domains_still_use_password_login(self, client, make_user):
        user = make_user(username="allowed", email="user@other.com")
        workspace = _workspace(user.id)
        _verified_domain(workspace.id, domain="example.com", enforce=True)

        response = client.post(
            "/auth/login",
            data={"email": "user@other.com", "password": "supersecret123"},
            follow_redirects=True,
        )

        assert b"Welcome back" in response.data
