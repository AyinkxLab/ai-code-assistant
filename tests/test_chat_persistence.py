"""Conversation persistence across reloads (#24)."""

import pytest

from app.models import Message
from app.services.providers import (
    ProviderConfigurationError,
    register_provider,
    unregister_provider,
)
from app.services.providers.base import LLMProvider, ProviderResponse


class _StreamingProvider(LLMProvider):
    name = "persist-test"
    models = ("persist-test-1",)

    def chat(self, messages, *, model=None, params=None):
        return ProviderResponse(content="complete", model=self.models[0])

    def stream(self, messages, *, model=None, params=None):
        for index in range(5):
            yield f"token{index} "


class _FailingProvider(LLMProvider):
    name = "persist-fail"
    models = ("persist-fail-1",)

    def chat(self, messages, *, model=None, params=None):
        raise ProviderConfigurationError("provider down", provider=self.name)

    def stream(self, messages, *, model=None, params=None):
        yield "partial "
        raise ProviderConfigurationError("provider down", provider=self.name)


@pytest.fixture()
def streaming_provider(monkeypatch):
    register_provider("persist-test", _StreamingProvider)
    monkeypatch.setenv("LLM_PROVIDER", "persist-test")
    yield
    unregister_provider("persist-test")


@pytest.fixture()
def failing_provider(monkeypatch):
    register_provider("persist-fail", _FailingProvider)
    monkeypatch.setenv("LLM_PROVIDER", "persist-fail")
    yield
    unregister_provider("persist-fail")


def _register(client):
    client.post(
        "/auth/register",
        data={
            "username": "persister",
            "email": "persister@example.com",
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _create_conversation(client):
    response = client.post(
        "/chat/conversations",
        json={},
        headers={"X-CSRFToken": "ignored"},
    )
    assert response.status_code == 201
    return response.get_json()


def _reload(client, conversation_id):
    response = client.get(f"/api/conversations/{conversation_id}")
    assert response.status_code == 200
    return response.get_json()


class TestReloadRestoresThread:
    def test_reload_returns_user_and_assistant_messages(self, client, streaming_provider):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hello"},
            headers={"X-CSRFToken": "ignored"},
        )
        response.get_data()

        payload = _reload(client, conversation["id"])

        assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
        assert payload["messages"][0]["content"] == "hello"
        assert payload["messages"][1]["content"].startswith("token0")
        assert payload["messages"][1]["interrupted"] is False


class TestInterruptedMessages:
    def test_failed_stream_persists_a_visible_interrupted_message(self, client, failing_provider):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hello"},
            headers={"X-CSRFToken": "ignored"},
        )
        response.get_data()

        payload = _reload(client, conversation["id"])
        assistant = [m for m in payload["messages"] if m["role"] == "assistant"]

        assert len(assistant) == 1
        assert assistant[0]["interrupted"] is True
        assert assistant[0]["content"].strip() == "partial"

    def test_user_message_survives_a_provider_failure(self, client, db, failing_provider):
        _register(client)
        conversation = _create_conversation(client)
        client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "still keep me"},
            headers={"X-CSRFToken": "ignored"},
        )

        payload = _reload(client, conversation["id"])
        user = [m for m in payload["messages"] if m["role"] == "user"]
        assert user[0]["content"] == "still keep me"


class TestNoDuplicateAssistantMessages:
    def test_cancelled_stream_reload_has_one_assistant_message(
        self, client, db, streaming_provider
    ):
        _register(client)
        conversation = _create_conversation(client)
        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "write a lot"},
            headers={"X-CSRFToken": "ignored"},
            buffered=False,
        )
        iterator = response.response
        next(iterator)
        iterator.close()  # simulate a refresh/abort mid-stream

        db.session.expire_all()
        payload = _reload(client, conversation["id"])
        assistant = [m for m in payload["messages"] if m["role"] == "assistant"]

        assert len(assistant) == 1
        assert assistant[0]["interrupted"] is True
        assert Message.query.filter_by(conversation_id=conversation["id"]).count() == 2
