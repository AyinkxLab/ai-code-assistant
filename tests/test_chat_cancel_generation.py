"""Cancel in-flight generation: partial content is kept (issue #11).

Covers the server side of "Stop": when the client disconnects mid-stream the
partial reply is persisted, and a provider failure mid-stream keeps the partial
text as well. The frontend Stop button/abort is exercised by the browser; these
tests pin the persistence guarantees it depends on.
"""

import pytest

from app.models import Conversation, Message
from app.services.providers import (
    ProviderConfigurationError,
    register_provider,
    unregister_provider,
)
from app.services.providers.base import LLMProvider, ProviderResponse


class _StreamingProvider(LLMProvider):
    """Yields many chunks so a test can cancel after the first ones."""

    name = "cancel-test"
    models = ("cancel-test-1",)

    def chat(self, messages, *, model=None, params=None):
        return ProviderResponse(content="complete", model=self.models[0])

    def stream(self, messages, *, model=None, params=None):
        for index in range(50):
            yield f"part{index} "


class _FailingProvider(LLMProvider):
    """Yields one chunk, then fails like a dropped provider connection."""

    name = "cancel-fail"
    models = ("cancel-fail-1",)

    def chat(self, messages, *, model=None, params=None):
        raise ProviderConfigurationError("boom", provider=self.name)

    def stream(self, messages, *, model=None, params=None):
        yield "half "
        raise ProviderConfigurationError("boom", provider=self.name)


@pytest.fixture()
def streaming_provider(monkeypatch):
    register_provider("cancel-test", _StreamingProvider)
    monkeypatch.setenv("LLM_PROVIDER", "cancel-test")
    yield
    unregister_provider("cancel-test")


@pytest.fixture()
def failing_provider(monkeypatch):
    register_provider("cancel-fail", _FailingProvider)
    monkeypatch.setenv("LLM_PROVIDER", "cancel-fail")
    yield
    unregister_provider("cancel-fail")


def _register(client):
    client.post(
        "/auth/register",
        data={
            "username": "canceller",
            "email": "canceller@example.com",
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


def _assistant_messages(db, conversation_id):
    stored = db.session.get(Conversation, conversation_id)
    return [m for m in stored.messages if m.role == "assistant"]


class TestCancelPersistsPartial:
    def test_closing_the_stream_persists_partial(self, client, db, streaming_provider):
        _register(client)
        conversation = _create_conversation(client)

        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "write a lot"},
            buffered=False,
        )
        iterator = response.response
        first = next(iterator)  # first SSE frame
        assert b"part0" in first
        iterator.close()  # simulate the client aborting (Stop button)

        db.session.expire_all()
        assistant = _assistant_messages(db, conversation["id"])
        assert len(assistant) == 1
        assert assistant[0].content.startswith("part0")
        # The conversation is still usable and not left in a broken state.
        assert Message.query.filter_by(conversation_id=conversation["id"]).count() == 2


class TestProviderFailureKeepsPartial:
    def test_mid_stream_failure_persists_partial_and_reports_error(
        self, client, db, failing_provider
    ):
        _register(client)
        conversation = _create_conversation(client)

        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hello"},
        )
        data = response.get_data(as_text=True)
        assert '"type": "error"' in data

        db.session.expire_all()
        assistant = _assistant_messages(db, conversation["id"])
        assert len(assistant) == 1
        assert assistant[0].content.strip() == "half"


class TestCompletedStream:
    def test_completed_stream_persists_all_chunks(self, client, db, streaming_provider):
        _register(client)
        conversation = _create_conversation(client)

        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "hello"},
        )
        response.get_data()

        db.session.expire_all()
        assistant = _assistant_messages(db, conversation["id"])
        assert len(assistant) == 1
        assert assistant[0].content.startswith("part0")
        assert "part49" in assistant[0].content
