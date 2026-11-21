"""Tests for the runnable SDK quickstart and its local mock server (#664)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "sdk" / "python" / "examples"
sys.path.insert(0, str(EXAMPLES))
sys.path.insert(0, str(ROOT / "sdk" / "python"))

import mock_server  # noqa: E402
import quickstart  # noqa: E402
from ai_code_assistant import AuthenticationError  # noqa: E402


def _running_server():
    server = mock_server.create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_quickstart_runs_against_the_mock_server():
    server, base_url = _running_server()
    try:
        summary = quickstart.run(base_url)
    finally:
        server.shutdown()
        server.server_close()

    assert summary["projects_page_1"] == ["demo-project-1", "demo-project-2"]
    assert summary["projects_page_2"] == ["demo-project-3", "demo-project-4"]
    assert summary["error"] == {"type": "NotFoundError", "status_code": 404}
    assert summary["webhook_deliveries"][0]["response_code"] == 503


def test_quickstart_rejects_a_missing_token():
    server, base_url = _running_server()
    try:
        with pytest.raises(AuthenticationError):
            quickstart.run(base_url, api_token="wrong-token")
    finally:
        server.shutdown()
        server.server_close()
