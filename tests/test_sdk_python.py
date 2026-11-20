"""Tests for the Phase 12 Python SDK (issues #657, #659, #661).

Every test runs against a local mock HTTP server, so the suite needs no network
access. The shared error-mapping fixture is asserted here and in the TypeScript
suite so both SDKs stay in agreement.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from ai_code_assistant import errors  # noqa: E402
from ai_code_assistant.client import Client  # noqa: E402
from ai_code_assistant.models import Project, Review, Usage  # noqa: E402

FIXTURE = json.loads((ROOT / "sdk" / "fixtures" / "error-mapping.json").read_text("utf-8"))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.calls.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        index = len(self.server.calls) - 1  # type: ignore[attr-defined]
        responses = self.server.responses  # type: ignore[attr-defined]
        response = responses[min(index, len(responses) - 1)]
        delay = response.get("delay", 0)
        if delay:
            time.sleep(delay)
        payload = response.get("body")
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
            content_type = "text/plain"
        else:
            raw = b""
            content_type = "text/plain"
        try:
            self.send_response(response.get("status", 200))
            self.send_header("Content-Type", response.get("content_type", content_type))
            self.send_header("Content-Length", str(len(raw)))
            for key, value in response.get("headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            if raw:
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


class _MockHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Timeout tests abort the connection mid-response; that is expected.
        return


class MockServer:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.server = _MockHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.responses = responses
        self.server.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.server.calls

    def __enter__(self) -> MockServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def make_client(server: MockServer, **kwargs: Any) -> Client:
    kwargs.setdefault("timeout", 5)
    kwargs.setdefault("max_retries", 2)
    kwargs.setdefault("backoff_factor", 0)
    kwargs.setdefault("jitter", False)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return Client(server.url, api_token="secret-token", **kwargs)


def test_sends_bearer_token_and_decodes_json() -> None:
    with MockServer([{"status": 200, "body": {"ok": True}}]) as server:
        response = make_client(server).get("/api/ping")

    assert response.status_code == 200
    assert response.data == {"ok": True}
    assert server.calls[0]["headers"]["authorization"] == "Bearer secret-token"
    assert server.calls[0]["headers"]["accept"] == "application/json"


def test_omits_authorization_header_without_token() -> None:
    with MockServer([{"status": 200, "body": {"ok": True}}]) as server:
        Client(server.url).get("/api/ping")

    assert "authorization" not in server.calls[0]["headers"]


def test_timeout_is_configurable_and_raises_typed_error() -> None:
    with MockServer([{"status": 200, "body": {"ok": True}, "delay": 0.3}]) as server:
        client = make_client(server, timeout=0.05, max_retries=0)
        with pytest.raises(errors.APITimeoutError):
            client.get("/api/slow")


def test_retries_idempotent_request_on_transient_status() -> None:
    responses = [
        {"status": 503, "body": {"error": "unavailable"}},
        {"status": 200, "body": {"ok": True}},
    ]
    with MockServer(responses) as server:
        response = make_client(server).get("/api/projects")

    assert response.status_code == 200
    assert len(server.calls) == 2


def test_never_retries_post() -> None:
    with MockServer([{"status": 503, "body": {"error": "unavailable"}}]) as server:
        client = make_client(server)
        with pytest.raises(errors.ServerError):
            client.post("/api/projects", json={"name": "demo"})

    assert len(server.calls) == 1


def test_does_not_retry_non_transient_status() -> None:
    with MockServer([{"status": 400, "body": {"error": "bad request"}}]) as server:
        client = make_client(server)
        with pytest.raises(errors.ValidationError):
            client.get("/api/projects")

    assert len(server.calls) == 1


def test_retries_are_bounded() -> None:
    with MockServer([{"status": 500, "body": {"error": "boom"}}]) as server:
        client = make_client(server, max_retries=2)
        with pytest.raises(errors.ServerError):
            client.get("/api/projects")

    assert len(server.calls) == 3


def test_retry_after_is_honored() -> None:
    responses = [
        {"status": 429, "body": {"error": "slow down"}, "headers": {"Retry-After": "2"}},
        {"status": 200, "body": {"ok": True}},
    ]
    delays: list[float] = []
    with MockServer(responses) as server:
        client = make_client(server, max_retries=1, max_backoff=5, sleep=delays.append)
        response = client.get("/api/projects")

    assert response.status_code == 200
    assert delays == [2.0]


def test_exponential_backoff_is_used() -> None:
    responses = [
        {"status": 500, "body": {"error": "boom"}},
        {"status": 500, "body": {"error": "boom"}},
        {"status": 200, "body": {"ok": True}},
    ]
    delays: list[float] = []
    with MockServer(responses) as server:
        client = make_client(server, max_retries=2, backoff_factor=0.5, sleep=delays.append)
        client.get("/api/projects")

    assert delays == [0.5, 1.0]


def test_preserves_code_status_and_request_id() -> None:
    body = {"error": {"code": "insufficient_scope", "message": "needs scope"}}
    with MockServer(
        [{"status": 403, "body": body, "headers": {"X-Request-ID": "req-123"}}]
    ) as server:
        client = make_client(server)
        with pytest.raises(errors.PermissionDeniedError) as info:
            client.get("/api/projects")

    error = info.value
    assert error.code == "insufficient_scope"
    assert error.request_id == "req-123"
    assert error.status_code == 403
    assert error.message == "needs scope"


def test_reads_request_id_from_body_when_header_missing() -> None:
    body = {"error": "gone", "code": "not_found", "request_id": "body-req"}
    with MockServer([{"status": 404, "body": body}]) as server:
        client = make_client(server)
        with pytest.raises(errors.NotFoundError) as info:
            client.get("/api/projects/9")

    assert info.value.request_id == "body-req"
    assert info.value.code == "not_found"


def test_status_mapping_matches_shared_fixture() -> None:
    for status, name in FIXTURE["status_to_error"].items():
        mapped = errors.error_from_response(int(status), {"error": "x"})
        assert type(mapped).__name__ == name

    for code, name in FIXTURE["code_to_error"].items():
        mapped = errors.error_from_response(418, {"error": {"code": code, "message": "x"}})
        assert type(mapped).__name__ == name


def test_retry_and_idempotency_sets_match_shared_fixture() -> None:
    assert set(FIXTURE["retryable_statuses"]) == set(errors.RETRYABLE_STATUSES)
    assert set(FIXTURE["idempotent_methods"]) == set(errors.IDEMPOTENT_METHODS)
    assert FIXTURE["request_id_header"] == errors.REQUEST_ID_HEADER


def test_models_parse_and_validate_payloads() -> None:
    project = Project.from_dict({"id": 1, "name": "Demo", "github_repo": "owner/repo"})
    assert project.name == "Demo"
    assert project.github_repo == "owner/repo"

    review = Review.from_dict(
        {
            "id": 2,
            "status": "complete",
            "quality_score": 9,
            "findings": [{"severity": "high", "title": "Unsafe query"}],
        }
    )
    assert review.quality_score == 9.0
    assert review.findings[0].title == "Unsafe query"

    usage = Usage.from_dict({"input_tokens": 10, "output_tokens": 5})
    assert usage.total_tokens == 15

    with pytest.raises(errors.ValidationError):
        Project.from_dict({"id": "1", "name": "Demo"})
    with pytest.raises(errors.ValidationError):
        Project.from_dict({"id": 1})
    with pytest.raises(errors.ValidationError):
        Review.from_dict({"id": 2, "status": "complete", "findings": "nope"})


def test_resource_helpers_parse_models_and_drop_none_params() -> None:
    responses = [
        {"status": 200, "body": {"items": [{"id": 1, "name": "Demo"}]}},
        {"status": 200, "body": {"id": 1, "name": "Demo"}},
        {"status": 200, "body": {"items": [{"id": 2, "status": "complete", "findings": []}]}},
        {"status": 200, "body": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}},
    ]
    with MockServer(responses) as server:
        client = make_client(server)
        assert client.list_projects()[0].name == "Demo"
        assert client.get_project(1).id == 1
        assert client.list_reviews(project_id=1)[0].status == "complete"
        assert client.get_usage().total_tokens == 3

    assert server.calls[0]["path"].startswith("/api/projects?")
    assert server.calls[2]["path"].endswith("project_id=1")
    assert "project_id" not in server.calls[3]["path"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"max_retries": -1},
        {"backoff_factor": -1},
        {"max_backoff": -1},
    ],
)
def test_rejects_invalid_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Client("https://api.example.com", **kwargs)


def test_rejects_invalid_base_url_and_resource_id() -> None:
    with pytest.raises(ValueError):
        Client("ftp://example.com")
    with pytest.raises(ValueError):
        Client("not-a-url")
    with pytest.raises(errors.ValidationError):
        Client("https://api.example.com").get_project(0)
