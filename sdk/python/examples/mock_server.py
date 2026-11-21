"""Local mock API for the SDK quickstart (#664).

Serves a tiny, deterministic subset of the public API over stdlib HTTP so the
quickstart can run without credentials or network access::

    python sdk/python/examples/mock_server.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parents[1]))

import quickstart  # noqa: E402  (local example module)

TOKEN = "demo-token"
PROJECTS = [
    {
        "id": index,
        "name": f"demo-project-{index}",
        "owner_id": 1,
        "description": None,
        "github_repo": None,
        "created_at": "2026-11-20T00:00:00+00:00",
        "updated_at": "2026-11-20T00:00:00+00:00",
    }
    for index in range(1, 5)
]
DELIVERIES = [
    {
        "id": 1,
        "subscription_id": 1,
        "event_type": "review.completed",
        "attempt": 1,
        "status": "failure",
        "response_code": 503,
        "latency_ms": 42,
        "error": "HTTP 503",
        "created_at": "2026-11-20T00:00:00+00:00",
    }
]


def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class MockApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if (
            parsed.path.startswith("/api/")
            and self.headers.get("Authorization") != f"Bearer {TOKEN}"
        ):
            _send(self, 401, {"code": "unauthenticated", "error": "missing or invalid token"})
            return

        if parsed.path == "/api/projects":
            query = parse_qs(parsed.query)
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["20"])[0])
            start = (page - 1) * per_page
            _send(
                self,
                200,
                {
                    "items": PROJECTS[start : start + per_page],
                    "total": len(PROJECTS),
                    "page": page,
                    "per_page": per_page,
                },
            )
            return

        if parsed.path.startswith("/api/projects/"):
            try:
                project_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                _send(self, 404, {"code": "not_found", "error": "project not found"})
                return
            project = next((item for item in PROJECTS if item["id"] == project_id), None)
            if project is None:
                _send(self, 404, {"code": "not_found", "error": "project not found"})
            else:
                _send(self, 200, project)
            return

        if parsed.path == "/webhooks/api/subscriptions/1/deliveries":
            _send(self, 200, {"items": DELIVERIES, "total": len(DELIVERIES)})
            return

        _send(self, 404, {"code": "not_found", "error": "unknown endpoint"})


def create_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Create (but do not start) a mock API server."""
    server = ThreadingHTTPServer((host, port), MockApiHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    server = create_server(port=8000)
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    print(f"Mock API listening on {base_url}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        quickstart.main(base_url=base_url, api_token=TOKEN)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
