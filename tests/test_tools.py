"""Tests for the AI tools blueprint: code generation, code actions, file analysis."""

from io import BytesIO

import pytest

from app.extensions import db as database
from app.models import FileAnalysis


def _register(client, username="tester", email="tester@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


class TestGenerate:
    def test_generate_requires_login(self, client):
        response = client.post("/tools/generate", json={})
        assert response.status_code == 302

    def test_generate_requires_description(self, client):
        _register(client)
        response = client.post("/tools/generate", json={}, headers={"X-CSRFToken": "ignored"})
        assert response.status_code == 400

    def test_generate_returns_mock_result(self, client):
        _register(client)
        response = client.post(
            "/tools/generate",
            json={"description": "Build a REST API in Flask", "language": "python"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["language"] == "python"
        assert "mock assistant response" in payload["result"].lower()


class TestCodeActions:
    @pytest.mark.parametrize(
        "action",
        ["explain", "refactor", "bugs", "optimize", "comments", "docs", "commit"],
    )
    def test_all_actions_work(self, client, action):
        _register(client)
        response = client.post(
            "/tools/code",
            json={"action": action, "code": "def foo():\n    return 1"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["action"] == action
        assert "mock assistant response" in payload["result"].lower()

    def test_unsupported_action(self, client):
        _register(client)
        response = client.post(
            "/tools/code",
            json={"action": "nonsense", "code": "x = 1"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400

    def test_code_required(self, client):
        _register(client)
        response = client.post(
            "/tools/code",
            json={"action": "explain", "code": ""},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400


class TestAnalyzeFile:
    def test_analysis_history_is_private_to_owner(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        created = client.post(
            "/tools/analyze",
            data={"file": (BytesIO(b"print('owner')"), "owner.py")},
            content_type="multipart/form-data",
        ).get_json()
        client.post("/auth/logout")
        _register(client, username="other", email="other@example.com")
        assert client.get("/tools/api/analyses").get_json() == []
        denied = client.delete(
            f"/tools/api/analyses/{created['analysis_id']}",
            headers={"X-CSRFToken": "ignored"},
        )
        assert denied.status_code == 404

    def test_analyze_python_file(self, client):
        _register(client)
        data = {
            "file": (BytesIO(b"def add(a, b):\n    return a + b"), "calc.py"),
        }
        response = client.post("/tools/analyze", data=data, content_type="multipart/form-data")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["filename"] == "calc.py"
        assert payload["action"] == "explain"
        assert "mock assistant response" in payload["result"].lower()

    def test_analyze_rejects_unsupported_type(self, client):
        _register(client)
        data = {"file": (BytesIO(b"binary\x00data"), "notes.exe")}
        response = client.post("/tools/analyze", data=data, content_type="multipart/form-data")
        assert response.status_code == 400

    def test_analyze_rejects_non_utf8(self, client):
        _register(client)
        data = {"file": (BytesIO(b"\xff\xfe\x00garbage"), "bad.txt")}
        response = client.post("/tools/analyze", data=data, content_type="multipart/form-data")
        assert response.status_code == 400

    def test_analyze_accepts_custom_action(self, client):
        _register(client)
        data = {
            "file": (BytesIO(b"print('hi')"), "main.py"),
            "action": "bugs",
        }
        response = client.post("/tools/analyze", data=data, content_type="multipart/form-data")
        assert response.status_code == 200
        assert response.get_json()["action"] == "bugs"

    def test_analysis_is_persisted_cached_and_deletable(self, client, db):
        _register(client)
        data = {"file": (BytesIO(b"print('persistent')"), "main.py")}
        first = client.post(
            "/tools/analyze", data=data, content_type="multipart/form-data"
        ).get_json()
        assert first["cached"] is False
        record = database.session.get(FileAnalysis, first["analysis_id"])
        assert record.file_id == first["file_id"]
        assert record.user_id is not None
        assert record.provider

        again = client.post(
            "/tools/analyze",
            data={"file": (BytesIO(b"print('persistent')"), "main.py")},
            content_type="multipart/form-data",
        ).get_json()
        assert again["cached"] is True
        assert again["analysis_id"] == first["analysis_id"]
        assert FileAnalysis.query.count() == 1

        history = client.get("/tools/history")
        assert history.status_code == 200
        assert b"main.py" in history.data
        removed = client.delete(
            f"/tools/api/analyses/{record.id}", headers={"X-CSRFToken": "ignored"}
        )
        assert removed.status_code == 200
        assert FileAnalysis.query.count() == 0


class TestSendToChat:
    def test_send_to_chat_creates_conversation(self, client, db):
        _register(client)
        response = client.post(
            "/tools/send-to-chat",
            json={"content": "Generated code here"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201
        assert "conversation_id" in response.get_json()

    def test_send_to_chat_requires_content(self, client):
        _register(client)
        response = client.post(
            "/tools/send-to-chat",
            json={"content": ""},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400
