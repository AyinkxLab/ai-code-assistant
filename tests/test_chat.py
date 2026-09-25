"""Tests for the chat blueprint: conversations, messages, streaming, export."""

from app.models import Conversation, ConversationShare, Notification, User


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


def _logout(client):
    client.post("/auth/logout")


def _create_conversation(client, title=None):
    payload = {"title": title} if title is not None else {}
    response = client.post(
        "/chat/conversations",
        json=payload,
        headers={"X-CSRFToken": "ignored"},
    )
    assert response.status_code == 201
    return response.get_json()


class TestChatPage:
    def test_chat_page_requires_login(self, client):
        response = client.get("/chat/")
        assert response.status_code == 302

    def test_chat_page_renders(self, client):
        _register(client)
        response = client.get("/chat/")
        assert response.status_code == 200
        assert b"Conversations" in response.data
        assert b"chat-input" in response.data

    def test_conversation_list_is_accessible(self, client, db):
        _register(client)
        _create_conversation(client, title="Accessible chat")
        html = client.get("/chat/").get_data(as_text=True)
        assert 'aria-label="Conversations"' in html
        assert 'role="log"' in html
        assert 'role="button"' in html
        assert 'tabindex="0"' in html
        assert 'aria-label="Open conversation: Accessible chat"' in html
        assert 'class="conversation-time"' in html


class TestConversationApi:
    def test_create_conversation(self, client, db):
        _register(client)
        data = _create_conversation(client, title="First chat")
        assert data["title"] == "First chat"
        assert data["message_count"] == 0

    def test_list_conversations_scoped_to_user(self, client, db):
        _register(client)
        _create_conversation(client, title="Mine")
        _logout(client)
        _register(client, username="other", email="other@example.com")
        response = client.get("/chat/conversations")
        assert response.status_code == 200
        assert response.get_json() == []

    def test_search_conversations(self, client, db):
        _register(client)
        _create_conversation(client, title="Refactor session")
        _create_conversation(client, title="Bug hunt")
        response = client.get("/chat/conversations?q=refactor")
        titles = [c["title"] for c in response.get_json()]
        assert titles == ["Refactor session"]

    def test_rename_conversation(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.patch(
            f"/chat/conversations/{conversation['id']}",
            json={"title": "Renamed"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.get_json()["title"] == "Renamed"

    def test_pin_conversation(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.patch(
            f"/chat/conversations/{conversation['id']}",
            json={"is_pinned": True},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.get_json()["is_pinned"] is True

    def test_delete_conversation(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.delete(
            f"/chat/conversations/{conversation['id']}",
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        assert Conversation.query.count() == 0

    def test_other_users_conversation_is_404(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        conversation = _create_conversation(client)
        _logout(client)
        _register(client, username="intruder", email="intruder@example.com")
        response = client.get(f"/chat/conversations/{conversation['id']}")
        assert response.status_code == 404


class TestMessageApi:
    def test_send_message_returns_mock_reply(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "Explain the strategy pattern"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201
        reply = response.get_json()["assistant_message"]
        assert reply["role"] == "assistant"
        assert "mock assistant response" in reply["content"].lower()

    def test_send_message_rejects_empty(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "   "},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400

    def test_first_message_sets_title(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "Help me build a CLI"},
            headers={"X-CSRFToken": "ignored"},
        )
        stored = db.session.get(Conversation, conversation["id"])
        assert stored.title == "Help me build a CLI"

    def test_history_is_returned(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "hi"},
            headers={"X-CSRFToken": "ignored"},
        )
        response = client.get(f"/chat/conversations/{conversation['id']}")
        messages = response.get_json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]


class TestStreaming:
    def test_stream_returns_sse_tokens_and_done(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "Write a Fibonacci function"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        data = response.get_data(as_text=True)
        assert "data: " in data
        assert '"type": "token"' in data
        assert '"type": "done"' in data

    def test_stream_persists_messages(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hello there"},
            headers={"X-CSRFToken": "ignored"},
        )
        # Consume the stream so the response generator runs and commits.
        response.get_data()
        stored = db.session.get(Conversation, conversation["id"])
        assert len(stored.messages) == 2

    def test_stream_rejects_empty(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": ""},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400

    def test_stream_surfaces_error_event_when_retries_exhausted(self, client, db, monkeypatch):
        from app.services.providers import ProviderUnavailableError

        class AlwaysFailingProvider:
            name = "failing"
            models = ("failing-1",)

            def stream(self, messages, *, model=None, params=None):
                raise ProviderUnavailableError("upstream down", provider=self.name)

            def complete(self, messages):
                raise ProviderUnavailableError("upstream down", provider=self.name)

        import app.chat.routes as chat_routes

        monkeypatch.setattr(chat_routes, "RetryingProvider", lambda provider, **kwargs: provider)
        monkeypatch.setattr(
            chat_routes, "build_provider", lambda user, name=None: AlwaysFailingProvider()
        )
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hello"},
            headers={"X-CSRFToken": "ignored"},
        )
        data = response.get_data(as_text=True)
        assert '"type": "error"' in data
        assert "upstream down" in data

    def test_stream_uses_retrying_provider(self, client, db, monkeypatch):
        from app.services.providers import ProviderResponse, ProviderUnavailableError
        from app.services.providers.retry import RetryingProvider

        class FlakyProvider:
            name = "flaky"
            models = ("flaky-1",)

            def __init__(self):
                self.stream_calls = 0

            def stream(self, messages, *, model=None, params=None):
                self.stream_calls += 1
                if self.stream_calls == 1:
                    raise ProviderUnavailableError("blip", provider=self.name)
                yield "recovered"

            def chat(self, messages, *, model=None, params=None):
                return ProviderResponse(content="recovered", model=self.models[0])

        provider = FlakyProvider()
        import app.chat.routes as chat_routes

        monkeypatch.setattr(
            chat_routes,
            "RetryingProvider",
            lambda wrapped, **kwargs: RetryingProvider(wrapped, sleep=lambda _delay: None),
        )
        monkeypatch.setattr(chat_routes, "build_provider", lambda user, name=None: provider)
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hello"},
            headers={"X-CSRFToken": "ignored"},
        )
        data = response.get_data(as_text=True)
        assert provider.stream_calls == 2
        assert '"type": "token"' in data
        assert '"type": "done"' in data


class TestExport:
    def test_export_returns_json_document(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Export me")
        client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "hi"},
            headers={"X-CSRFToken": "ignored"},
        )
        response = client.get(f"/chat/conversations/{conversation['id']}/export")
        assert response.status_code == 200
        assert response.mimetype == "application/json"
        assert "attachment" in response.headers["Content-Disposition"]
        payload = response.get_json()
        assert payload["conversation"]["title"] == "Export me"
        assert len(payload["messages"]) == 2


class TestShareApi:
    def _share(self, client, conversation_id, username):
        return client.post(
            f"/chat/conversations/{conversation_id}/shares",
            json={"username": username},
            headers={"X-CSRFToken": "ignored"},
        )

    def _unshare(self, client, conversation_id, user_id):
        return client.delete(
            f"/chat/conversations/{conversation_id}/shares/{user_id}",
            headers={"X-CSRFToken": "ignored"},
        )

    def test_share_creates_share_record_and_notification(self, client, db, login):
        _register(client, username="owner1", email="owner1@example.com")
        conversation = _create_conversation(client, title="Shared gem")
        _logout(client)
        _register(client, username="recipient", email="recipient@example.com")
        recipient = User.query.filter_by(username="recipient").one()
        login(email="owner1@example.com")

        response = self._share(client, conversation["id"], "recipient")
        assert response.status_code == 201
        share = ConversationShare.query.filter_by(conversation_id=conversation["id"]).one()
        assert share.user_id == recipient.id
        assert share.shared_by_id is not None
        notification = Notification.query.filter_by(type="share", user_id=recipient.id).one()
        assert notification.payload["conversation_id"] == conversation["id"]
        assert notification.payload["title"] == "Shared gem"
        assert notification.is_read is False

    def test_recipient_can_read_shared_conversation(self, client, db, login):
        _register(client, username="owner", email="owner@example.com")
        conversation = _create_conversation(client, title="Readable")
        _logout(client)
        _register(client, username="recipient", email="recipient@example.com")
        _logout(client)
        _register(client, username="unrelated", email="unrelated@example.com")
        login(email="owner@example.com")
        self._share(client, conversation["id"], "recipient")
        login(email="recipient@example.com")

        response = client.get(f"/chat/conversations/{conversation['id']}")
        assert response.status_code == 200
        assert response.get_json()["title"] == "Readable"
        listed = [c["id"] for c in client.get("/chat/conversations").get_json()]
        assert conversation["id"] in listed

    def test_unshared_user_cannot_read(self, client, db, login):
        _register(client, username="owner", email="owner@example.com")
        conversation = _create_conversation(client)
        _logout(client)
        _register(client, username="recipient", email="recipient@example.com")
        _logout(client)
        _register(client, username="stranger", email="stranger@example.com")
        login(email="owner@example.com")
        self._share(client, conversation["id"], "recipient")
        login(email="stranger@example.com")
        response = client.get(f"/chat/conversations/{conversation['id']}")
        assert response.status_code == 404
        assert conversation["id"] not in [
            c["id"] for c in client.get("/chat/conversations").get_json()
        ]

    def test_share_is_owner_only(self, client, db, login):
        _register(client, username="owner", email="owner@example.com")
        conversation = _create_conversation(client)
        _logout(client)
        _register(client, username="recipient", email="recipient@example.com")
        login(email="owner@example.com")
        self._share(client, conversation["id"], "recipient")
        login(email="recipient@example.com")

        response = self._share(client, conversation["id"], "owner")
        assert response.status_code == 404

    def test_share_unknown_user_404(self, client, db, login):
        _register(client, username="owner3", email="owner3@example.com")
        conversation = _create_conversation(client)
        response = self._share(client, conversation["id"], "ghost")
        assert response.status_code == 404

    def test_share_self_rejected(self, client, db, login):
        _register(client, username="owner4", email="owner4@example.com")
        conversation = _create_conversation(client)
        response = self._share(client, conversation["id"], "owner4")
        assert response.status_code == 400

    def test_duplicate_share_409(self, client, db, login):
        _register(client, username="owner", email="owner@example.com")
        conversation = _create_conversation(client)
        _logout(client)
        _register(client, username="recipient", email="recipient@example.com")
        login(email="owner@example.com")
        assert self._share(client, conversation["id"], "recipient").status_code == 201
        assert self._share(client, conversation["id"], "recipient").status_code == 409

    def test_unshare_revokes_access(self, client, db, login):
        _register(client, username="owner", email="owner@example.com")
        conversation = _create_conversation(client)
        _logout(client)
        _register(client, username="recipient", email="recipient@example.com")
        login(email="owner@example.com")
        share = self._share(client, conversation["id"], "recipient").get_json()
        response = self._unshare(client, conversation["id"], share["user_id"])
        assert response.status_code == 200
        assert ConversationShare.query.count() == 0
        login(email="recipient@example.com")
        assert client.get(f"/chat/conversations/{conversation['id']}").status_code == 404

    def test_share_requires_login(self, client):
        assert (
            client.post("/chat/conversations/1/shares", json={"username": "x"}).status_code == 302
        )
