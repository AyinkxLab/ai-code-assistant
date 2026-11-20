"""Loopback-only integration tests for the connectors in this repository (#685).

GitHub is exercised against an in-process HTTP server bound to 127.0.0.1:
the real ``GitHubClient``, token refresh, and revocation code make actual
loopback requests. Stellar is exercised against the existing deterministic
``MockStellarServer``. No external network is used.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.github import (
    GitHubClient,
    GitHubNotFoundError,
    _refresh_github_token,
    revoke_github_token,
)
from app.services.soroban_rpc import SorobanRpcClient
from app.services.stellar import StellarService
from app.services.stellar_mock import MOCK_ACCOUNT, MOCK_LATEST_LEDGER

REPO = {
    "id": 1,
    "full_name": "octo/demo",
    "name": "demo",
    "private": False,
    "default_branch": "main",
    "owner": {"login": "octo"},
}
USER = {"id": 42, "login": "octo", "name": "Octo Cat"}


class _GitHubHandler(BaseHTTPRequestHandler):
    """Deterministic subset of the GitHub REST and OAuth endpoints."""

    server_version = "MockGitHub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean and deterministic
        pass

    def _reply(self, status, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        self.server.requests.append(
            {"method": self.command, "path": urlsplit(self.path).path, "body": body}
        )
        return body

    def do_GET(self):
        self._record()
        path = urlsplit(self.path).path
        if path == "/user":
            self._reply(200, USER, {"X-OAuth-Scopes": "repo, read:user"})
        elif path == "/user/repos":
            self._reply(200, [REPO])
        elif path == "/repos/octo/demo":
            self._reply(200, REPO)
        elif path == "/repos/octo/missing":
            self._reply(404, {"message": "Not Found"})
        else:
            self._reply(404, {"message": "Not Found"})

    def do_POST(self):
        body = self._record()
        if urlsplit(self.path).path == "/login/oauth/access_token":
            form = parse_qs(body)
            assert form["client_id"] == ["client-id"]
            self._reply(200, {"access_token": "gho_refreshed", "token_type": "bearer"})
        else:
            self._reply(404, {"message": "Not Found"})

    def do_DELETE(self):
        self._record()
        if urlsplit(self.path).path == "/applications/client-id/token":
            self._reply(204)
        else:
            self._reply(404, {"message": "Not Found"})


class MockGitHubServer:
    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubHandler)
        self._server.requests = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

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
def mock_github():
    server = MockGitHubServer().start()
    try:
        yield server
    finally:
        server.stop()


class TestGitHubLoopback:
    def test_server_is_loopback_only(self, mock_github):
        assert mock_github.url.startswith("http://127.0.0.1:")

    def test_client_reads_user_repositories_and_scopes(self, mock_github):
        client = GitHubClient("gho_token", api_url=mock_github.url, max_retries=1)

        assert client.get_user()["login"] == "octo"
        assert client.get_granted_scopes() == ["repo", "read:user"]
        assert [repo["full_name"] for repo in client.list_repositories()] == ["octo/demo"]

    def test_missing_repository_returns_a_typed_error(self, mock_github):
        client = GitHubClient("gho_token", api_url=mock_github.url, max_retries=1)

        with pytest.raises(GitHubNotFoundError):
            client.get_repository("octo/missing")

    def test_token_refresh_uses_the_loopback_exchange(self, app, mock_github, monkeypatch):
        monkeypatch.setattr(
            "app.services.github.GITHUB_TOKEN_URL",
            f"{mock_github.url}/login/oauth/access_token",
        )
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"

        refreshed = _refresh_github_token("ghr_old")

        assert refreshed["access_token"] == "gho_refreshed"
        assert mock_github.requests[-1]["path"] == "/login/oauth/access_token"

    def test_revocation_calls_the_loopback_endpoint(self, app, mock_github, monkeypatch):
        monkeypatch.setattr(
            "app.services.github.GITHUB_REVOKE_URL",
            mock_github.url + "/applications/{client_id}/token",
        )
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"

        revoke_github_token("gho_revoke_me")

        request = mock_github.requests[-1]
        assert request["method"] == "DELETE"
        assert request["path"] == "/applications/client-id/token"
        assert json.loads(request["body"]) == {"access_token": "gho_revoke_me"}


class TestStellarLoopback:
    def test_horizon_and_rpc_against_the_mock(self, app, mock_stellar):
        app.config.update(
            STELLAR_NETWORK="custom",
            STELLAR_HORIZON_URL=mock_stellar.horizon_url,
            STELLAR_RPC_URL=mock_stellar.rpc_url,
        )
        assert mock_stellar.horizon_url.startswith("http://127.0.0.1:")
        assert mock_stellar.rpc_url.startswith("http://127.0.0.1:")

        with app.app_context():
            account = StellarService().get_account(MOCK_ACCOUNT)
            health = SorobanRpcClient().get_health()

        assert account["account_id"] == MOCK_ACCOUNT
        assert health["status"] == "healthy"
        assert health["latestLedger"] == MOCK_LATEST_LEDGER
