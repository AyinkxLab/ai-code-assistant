"""Tests for the chat JSON API (``/api/conversations``, issue #5)."""

from app.models import Conversation


def _register(client, username="apiuser", email="apiuser@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _create_conversation(client, title=None):
    payload = {"title": title} if title is not None else {}
    return client.post(
        "/api/conversations",
        json=payload,
        headers={"X-CSRFToken": "ignored"},
    )


def _problem(response):
    """Return the parsed RFC 7807 body and assert the response is one."""
    assert response.mimetype == "application/problem+json"
    payload = response.get_json()
    assert payload["type"] == "about:blank"
    assert payload["status"] == response.status_code
    assert payload["title"]
    return payload


class TestAuthentication:
    def test_requires_authentication(self, client):
        response = client.get("/api/conversations")
        assert response.status_code == 401
        _problem(response)

    def test_create_requires_authentication(self, client):
        response = client.post("/api/conversations", json={})
        assert response.status_code == 401
        _problem(response)


class TestConversationEndpoints:
    def test_create_conversation(self, client, db):
        _register(client)
        response = _create_conversation(client, title="First chat")
        assert response.status_code == 201
        data = response.get_json()
        assert data["title"] == "First chat"
        assert data["message_count"] == 0

    def test_list_conversations_newest_first(self, client, db):
        _register(client)
        _create_conversation(client, title="Older")
        _create_conversation(client, title="Newer")
        data = client.get("/api/conversations").get_json()
        assert [c["title"] for c in data] == ["Newer", "Older"]

    def test_list_is_owner_scoped(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        _create_conversation(client)
        client.post("/auth/logout")
        _register(client, username="other", email="other@example.com")
        assert client.get("/api/conversations").get_json() == []

    def test_get_includes_messages(self, client, db):
        _register(client)
        created = _create_conversation(client).get_json()
        client.post(
            f"/api/conversations/{created['id']}/messages",
            json={"content": "Explain the strategy pattern"},
            headers={"X-CSRFToken": "ignored"},
        )
        data = client.get(f"/api/conversations/{created['id']}").get_json()
        assert [m["role"] for m in data["messages"]] == ["user", "assistant"]

    def test_delete_removes_conversation(self, client, db):
        _register(client)
        created = _create_conversation(client).get_json()
        response = client.delete(
            f"/api/conversations/{created['id']}",
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        assert response.get_json() == {"ok": True}
        assert Conversation.query.count() == 0

    def test_non_owner_gets_404_without_leaking(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        created = _create_conversation(client, title="Secret").get_json()
        client.post("/auth/logout")
        _register(client, username="intruder", email="intruder@example.com")

        detail = client.get(f"/api/conversations/{created['id']}")
        assert detail.status_code == 404
        _problem(detail)
        assert "Secret" not in detail.get_data(as_text=True)

        delete = client.delete(
            f"/api/conversations/{created['id']}",
            headers={"X-CSRFToken": "ignored"},
        )
        assert delete.status_code == 404
        assert db.session.get(Conversation, created["id"]) is not None


class TestMessageEndpoint:
    def test_send_message_returns_reply(self, client, db):
        _register(client)
        created = _create_conversation(client).get_json()
        response = client.post(
            f"/api/conversations/{created['id']}/messages",
            json={"content": "Write a haiku about tests"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201
        reply = response.get_json()["assistant_message"]
        assert reply["role"] == "assistant"
        assert reply["content"]

    def test_empty_message_is_rejected(self, client, db):
        _register(client)
        created = _create_conversation(client).get_json()
        response = client.post(
            f"/api/conversations/{created['id']}/messages",
            json={"content": "   "},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400
        payload = _problem(response)
        assert "content" in payload["detail"].lower()

    def test_invalid_body_is_rejected(self, client, db):
        _register(client)
        created = _create_conversation(client).get_json()
        response = client.post(
            f"/api/conversations/{created['id']}/messages",
            data="not json",
            content_type="application/json",
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400
        _problem(response)

    def test_send_message_is_owner_scoped(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        created = _create_conversation(client).get_json()
        client.post("/auth/logout")
        _register(client, username="intruder", email="intruder@example.com")
        response = client.post(
            f"/api/conversations/{created['id']}/messages",
            json={"content": "hi"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 404
        _problem(response)


class TestRateLimit:
    def test_per_user_limit_returns_429_problem(self, client, db, app):
        app.config["RATE_LIMIT_CHAT_MAX"] = 1
        app.config["RATE_LIMIT_CHAT_WINDOW"] = 60
        _register(client)

        assert client.get("/api/conversations").status_code == 200
        response = client.get("/api/conversations")
        assert response.status_code == 429
        _problem(response)
        assert response.headers["Retry-After"]
