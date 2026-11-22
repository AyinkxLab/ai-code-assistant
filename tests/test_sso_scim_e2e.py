"""End-to-end coverage for the Phase 11 SSO and SCIM surface (#639).

This repository does not implement an OIDC/SAML relying party or a SCIM 2.0
server yet; see ``docs/sso-scim-setup.md``. The tests below therefore exercise
the supported end-to-end lifecycle rather than a vendor flow:

* owner sign-in -> DNS domain challenge -> verification -> SSO enforcement,
* password sign-in refused for an enforced domain and the refusal audited,
* workspace provisioning through the member API -> role mapping,
* deprovisioning (soft removal, then account deactivation) withdrawing access
  and revoking an existing browser session.

A loopback-only mock identity provider (discovery/JWKS/SAML metadata/SCIM) is
kept ready for the protocol round trip, which is skipped until those endpoints
exist. No external network or live vendor is contacted.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from app.extensions import db
from app.models import AuditLog, User, Workspace, WorkspaceDomain, WorkspaceMember
from app.models.workspace_member import STATUS_REMOVED
from app.services import audit, domain_verification


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


def _login_as(client, email, password="supersecret123"):
    return client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=True,
    )


class TestSupportedSSOEnforcement:
    def test_domain_verification_enforcement_and_password_block(
        self, client, make_user, login
    ):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _workspace(owner.id)
        login(email="owner@example.com")

        challenge_response = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "Example.com"},
        )
        assert challenge_response.status_code == 201
        challenge = challenge_response.get_json()
        assert challenge["domain"] == "example.com"
        assert challenge["txt_record_name"] == "_aica-verification.example.com"
        assert challenge["txt_record_value"].startswith("aica-verify=")

        with patch.object(
            domain_verification,
            "_resolve_txt",
            return_value=[challenge["txt_record_value"]],
        ):
            verified = client.post(
                "/auth/api/domains/verify",
                json={"workspace_id": workspace.id, "domain": "example.com"},
            )
        assert verified.status_code == 200
        assert verified.get_json()["verified_at"] is not None

        enforced = client.patch(
            f"/auth/api/domains/{challenge['id']}",
            json={"workspace_id": workspace.id, "enforce_sso": True},
        )
        assert enforced.status_code == 200
        assert enforced.get_json()["enforce_sso"] is True

        member = make_user(username="member", email="member@example.com")
        client.post("/auth/logout")
        blocked = _login_as(client, "member@example.com")

        assert blocked.status_code == 200
        assert b"Password sign-in is disabled" in blocked.data
        assert client.get("/auth/me").status_code == 302

        failure = (
            AuditLog.query.filter_by(action=audit.LOGIN_FAILURE, user_id=member.id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert failure is not None
        assert failure.metadata_dict["reason"] == "sso_enforced"

    def test_disabling_enforcement_restores_password_login(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner.id)
        login(email="owner@example.com")
        row = _verified_domain(workspace.id, enforce=True)

        disabled = client.patch(
            f"/auth/api/domains/{row.id}",
            json={"workspace_id": workspace.id, "enforce_sso": False},
        )
        assert disabled.status_code == 200

        client.post("/auth/logout")
        allowed = _login_as(client, member.email)
        assert b"Welcome back" in allowed.data
        assert client.get("/auth/me").status_code == 200

    def test_unverified_domain_cannot_be_enforced(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _workspace(owner.id)
        login(email="owner@example.com")
        challenge = client.post(
            "/auth/api/domains/challenge",
            json={"workspace_id": workspace.id, "domain": "example.com"},
        ).get_json()

        response = client.patch(
            f"/auth/api/domains/{challenge['id']}",
            json={"workspace_id": workspace.id, "enforce_sso": True},
        )

        assert response.status_code == 409
        assert response.get_json()["code"] == "conflict"

    def test_expired_challenge_returns_gone(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _workspace(owner.id)
        row = WorkspaceDomain(
            workspace_id=workspace.id,
            domain="example.com",
            verification_token="token",
            challenge_expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        db.session.add(row)
        db.session.commit()
        login(email="owner@example.com")

        response = client.post(
            "/auth/api/domains/verify",
            json={"workspace_id": workspace.id, "domain": "example.com"},
        )

        assert response.status_code == 410
        assert response.get_json()["code"] == "gone"


class TestProvisioningRoleMappingAndDeprovisioning:
    def test_provision_map_role_remove_and_revoke_session(
        self, app, client, make_user, login
    ):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@partner.test")
        workspace = _workspace(owner.id)
        login(email="owner@example.com")

        provisioned = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "member", "role": "viewer"},
        )
        assert provisioned.status_code == 201
        assert provisioned.get_json()["role"] == "viewer"

        mapped = client.patch(
            f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}",
            json={"role": "contributor"},
        )
        assert mapped.status_code == 200
        assert mapped.get_json()["role"] == "contributor"

        member_client = app.test_client()
        assert b"Welcome back" in _login_as(member_client, "member@partner.test").data
        members_path = f"/workspaces/api/workspaces/{workspace.id}/members"
        assert member_client.get(members_path).status_code == 200

        removed = client.delete(
            f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}"
        )
        assert removed.status_code == 200
        membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=member.id
        ).one()
        assert membership.status == STATUS_REMOVED

        # The member's existing session survives the workspace removal but no
        # longer authorizes the workspace.
        assert member_client.get(members_path).status_code == 404

        # Deprovisioning the account itself revokes that existing session.
        member.is_active = False
        db.session.commit()
        assert member_client.get("/auth/me").status_code == 302

        refused = _login_as(member_client, "member@partner.test")
        assert b"account has been disabled" in refused.data
        assert User.query.filter_by(email="member@partner.test").one().is_active is False


class _IdentityProviderHandler(BaseHTTPRequestHandler):
    """Deterministic loopback OIDC, SAML metadata, and SCIM endpoints."""

    server_version = "MockIdentityProvider/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, payload=None, content_type="application/json"):
        if payload is None:
            body = b""
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _record(self):
        self.server.requests.append({"method": self.command, "path": urlsplit(self.path).path})

    def do_GET(self):
        self._record()
        path = urlsplit(self.path).path
        issuer = self.server.issuer
        if path == "/.well-known/openid-configuration":
            self._reply(
                200,
                {
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/oauth/authorize",
                    "token_endpoint": f"{issuer}/oauth/token",
                    "userinfo_endpoint": f"{issuer}/oauth/userinfo",
                    "jwks_uri": f"{issuer}/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "scopes_supported": ["openid", "profile", "email"],
                },
            )
        elif path == "/jwks":
            self._reply(200, {"keys": []})
        elif path == "/saml/metadata":
            self._reply(
                200,
                b'<EntityDescriptor entityID="mock-idp"></EntityDescriptor>',
                "application/samlmetadata+xml",
            )
        elif path == "/scim/v2/ServiceProviderConfig":
            self._reply(
                200,
                {
                    "schemas": [
                        "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
                    ]
                },
            )
        elif path == "/scim/v2/Users":
            self._reply(
                200,
                {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
                    "totalResults": 0,
                    "Resources": [],
                },
            )
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self):
        self._record()
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path == "/oauth/token":
            self._reply(200, {"access_token": "mock-access-token", "token_type": "Bearer"})
        elif path == "/scim/v2/Users":
            self._reply(201, {"id": "mock-user", "active": True})
        else:
            self._reply(404, {"error": "not_found"})

    def do_DELETE(self):
        self._record()
        if urlsplit(self.path).path.startswith("/scim/v2/Users/"):
            self._reply(204)
        else:
            self._reply(404, {"error": "not_found"})


class MockIdentityProvider:
    """Loopback-only stand-in for a vendor IdP/SCIM endpoint."""

    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _IdentityProviderHandler)
        self._server.requests = []
        host, port = self._server.server_address[:2]
        self._server.issuer = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return self._server.issuer

    @property
    def requests(self):
        return self._server.requests

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def mock_idp():
    server = MockIdentityProvider().start()
    try:
        yield server
    finally:
        server.stop()


class TestLoopbackIdentityProvider:
    def test_mock_idp_is_loopback_only_and_serves_protocol_documents(self, mock_idp):
        assert mock_idp.url.startswith("http://127.0.0.1:")

        discovery = requests.get(
            f"{mock_idp.url}/.well-known/openid-configuration", timeout=5
        ).json()
        assert discovery["issuer"] == mock_idp.url
        assert discovery["jwks_uri"] == f"{mock_idp.url}/jwks"

        scim = requests.get(f"{mock_idp.url}/scim/v2/Users", timeout=5).json()
        assert scim["totalResults"] == 0

    @pytest.mark.skip(
        reason=(
            "OIDC/SAML/SCIM protocol endpoints and JIT provisioning are not "
            "implemented in this repository; see docs/sso-scim-setup.md"
        )
    )
    def test_sso_login_jit_group_mapping_and_scim_deprovision(self, client, mock_idp):
        """Protocol round trip to enable once the relying party/SCIM server lands.

        Expected sequence: OIDC/SAML login against ``mock_idp`` -> JIT-create the
        user -> map an IdP group to a workspace role -> SCIM DELETE deactivates
        the user -> the existing session is revoked.
        """
