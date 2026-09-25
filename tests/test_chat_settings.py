"""Tests for per-conversation model and generation settings (issue #12).

Covers the provider/model options endpoint, persistence and validation of
settings, and that a conversation's chosen model, temperature, and system
prompt are what the chat/stream endpoints actually send.
"""

from app.services.api_keys import create_key
from app.services.providers import ProviderResponse


def _register(client):
    client.post(
        "/auth/register",
        data={
            "username": "setter",
            "email": "setter@example.com",
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _create_conversation(client, **settings):
    response = client.post(
        "/chat/conversations",
        json=settings,
        headers={"X-CSRFToken": "ignored"},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _set_settings(client, conversation_id, payload):
    return client.patch(
        f"/chat/conversations/{conversation_id}/settings",
        json=payload,
        headers={"X-CSRFToken": "ignored"},
    )


class FakeProvider:
    name = "fake"
    models = ("fake-1",)

    def __init__(self):
        self.chat_calls = []
        self.stream_calls = []

    def chat(self, messages, *, model=None, params=None):
        self.chat_calls.append({"messages": messages, "model": model, "params": params})
        return ProviderResponse(content="fake reply", model=model)

    def stream(self, messages, *, model=None, params=None):
        self.stream_calls.append({"messages": messages, "model": model, "params": params})
        yield "fake "
        yield "reply"


class TestProviderOptions:
    def test_lists_registered_providers(self, client, db, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _register(client)
        data = client.get("/chat/api/options").get_json()
        by_name = {provider["name"]: provider for provider in data["providers"]}
        assert "mock" in by_name
        assert by_name["mock"]["available"] is True
        assert "mock-1" in by_name["mock"]["models"]
        assert by_name["openai"]["available"] is False
        assert by_name["openai"]["requires_key"] is True
        assert data["default_temperature"] > 0

    def test_active_user_key_makes_provider_available(
        self, client, db, make_user, login, monkeypatch
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        user = make_user()
        login()
        create_key(user, provider="openai", secret="sk-test")
        data = client.get("/chat/api/options").get_json()
        by_name = {provider["name"]: provider for provider in data["providers"]}
        assert by_name["openai"]["available"] is True
        assert by_name["openai"]["has_key"] is True


class TestConversationSettings:
    def test_create_conversation_with_settings(self, client, db):
        _register(client)
        conversation = _create_conversation(
            client,
            provider="mock",
            model="mock-1",
            temperature=0.4,
            system_prompt="Be concise.",
        )
        assert conversation["provider"] == "mock"
        assert conversation["model"] == "mock-1"
        assert conversation["temperature"] == 0.4
        assert conversation["system_prompt"] == "Be concise."

    def test_settings_persist_and_are_returned(self, client, db, make_user, login, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        make_user()
        login()
        conversation = _create_conversation(client)
        response = _set_settings(
            client,
            conversation["id"],
            {
                "provider": "mock",
                "model": "mock-1",
                "temperature": 0.2,
                "system_prompt": "Always cite sources.",
            },
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["model"] == "mock-1"
        assert payload["temperature"] == 0.2
        assert payload["system_prompt"] == "Always cite sources."

        fetched = client.get(f"/chat/conversations/{conversation['id']}").get_json()
        assert fetched["provider"] == "mock"
        assert fetched["model"] == "mock-1"
        assert fetched["temperature"] == 0.2
        assert fetched["system_prompt"] == "Always cite sources."

    def test_changing_settings_does_not_require_new_conversation(
        self, client, db, make_user, login
    ):
        make_user()
        login()
        conversation = _create_conversation(client)
        assert _set_settings(client, conversation["id"], {"temperature": 1.1}).status_code == 200
        response = client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "hello"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201
        assert (
            len(client.get(f"/chat/conversations/{conversation['id']}").get_json()["messages"]) == 2
        )

    def test_invalid_temperature_rejected(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        assert _set_settings(client, conversation["id"], {"temperature": 9}).status_code == 400
        assert _set_settings(client, conversation["id"], {"temperature": "hot"}).status_code == 400

    def test_unsupported_model_rejected(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        response = _set_settings(
            client, conversation["id"], {"provider": "mock", "model": "gpt-4o-mini"}
        )
        assert response.status_code == 400
        assert "supported" in response.get_json()["error"].lower()

    def test_unavailable_provider_rejected(self, client, db, make_user, login, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        make_user()
        login()
        conversation = _create_conversation(client)
        response = _set_settings(client, conversation["id"], {"provider": "openai"})
        assert response.status_code == 400
        assert "not available" in response.get_json()["error"].lower()

    def test_long_system_prompt_rejected(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        response = _set_settings(client, conversation["id"], {"system_prompt": "x" * 4001})
        assert response.status_code == 400


class TestSettingsReachProvider:
    def test_send_message_uses_selected_model_and_system_prompt(
        self, client, db, make_user, login, monkeypatch
    ):
        fake = FakeProvider()
        monkeypatch.setattr("app.chat.routes.build_provider", lambda user, name=None: fake)
        make_user()
        login()
        conversation = _create_conversation(client)
        _set_settings(
            client,
            conversation["id"],
            {"provider": "mock", "model": "mock-1", "temperature": 0.3, "system_prompt": "SYS"},
        )

        response = client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "hi"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201
        call = fake.chat_calls[-1]
        assert call["model"] == "mock-1"
        assert call["params"] == {"temperature": 0.3}
        assert call["messages"][0] == {"role": "system", "content": "SYS"}
        assert call["messages"][-1] == {"role": "user", "content": "hi"}

    def test_stream_uses_selected_settings(self, client, db, make_user, login, monkeypatch):
        fake = FakeProvider()
        monkeypatch.setattr("app.chat.routes.build_provider", lambda user, name=None: fake)
        make_user()
        login()
        conversation = _create_conversation(client)
        _set_settings(
            client,
            conversation["id"],
            {"provider": "mock", "model": "mock-1", "temperature": 0.6, "system_prompt": "SYS"},
        )

        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hi"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert '"type": "token"' in text
        assert '"type": "done"' in text
        stream_call = fake.stream_calls[-1]
        assert stream_call["model"] == "mock-1"
        assert stream_call["params"] == {"temperature": 0.6}
        assert stream_call["messages"][0] == {"role": "system", "content": "SYS"}
