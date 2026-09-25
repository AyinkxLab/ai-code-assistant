"""Graceful onboarding when no usable provider key is configured (issue #25).

Covers the pre-flight provider check, the distinct ``provider_not_configured``
error code, the onboarding panel, and live unlocking once a user stores a key.
"""

from app.extensions import db
from app.models import Conversation
from app.services.api_keys import create_key
from app.services.llm import get_provider


def _force_provider_without_key(monkeypatch):
    """Select the OpenAI provider and guarantee no environment key is present."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _new_conversation(user):
    conversation = Conversation(user_id=user.id, title="Chat")
    db.session.add(conversation)
    db.session.commit()
    return conversation


class TestProviderStatusEndpoint:
    def test_reports_missing_key(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        _force_provider_without_key(monkeypatch)

        body = client.get("/chat/api/provider-status").get_json()

        assert body["provider"] == "openai"
        assert body["requires_key"] is True
        assert body["configured"] is False
        assert body["reason"] == "missing_key"

    def test_reports_configured_from_environment(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

        body = client.get("/chat/api/provider-status").get_json()

        assert body["configured"] is True
        assert body["reason"] == "environment"

    def test_reports_configured_from_stored_key(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        _force_provider_without_key(monkeypatch)
        create_key(user, provider="openai", secret="sk-stored")

        body = client.get("/chat/api/provider-status").get_json()

        assert body["configured"] is True
        assert body["reason"] == "stored_key"

    def test_mock_provider_needs_no_key(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setenv("LLM_PROVIDER", "mock")

        response = client.get("/chat/api/provider-status")
        assert response.status_code == 200, (response.status_code, response.get_data(as_text=True))
        body = response.get_json()

        assert body["requires_key"] is False
        assert body["configured"] is True


class TestOnboardingPanel:
    def test_chat_page_shows_panel_without_key(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        _force_provider_without_key(monkeypatch)

        html = client.get("/chat/").get_data(as_text=True)

        assert 'id="provider-onboarding"' in html
        assert 'data-provider="openai"' in html
        assert "/keys/" in html

    def test_chat_page_hides_panel_with_stored_key(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        _force_provider_without_key(monkeypatch)
        create_key(user, provider="openai", secret="sk-stored")

        html = client.get("/chat/").get_data(as_text=True)

        assert 'id="provider-onboarding"' not in html


class TestSendBlockedWithoutKey:
    def test_non_stream_returns_distinct_code(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        _force_provider_without_key(monkeypatch)
        conversation = _new_conversation(user)

        response = client.post(
            f"/chat/conversations/{conversation.id}/messages",
            json={"content": "hello"},
        )

        assert response.status_code == 503
        body = response.get_json()
        assert body["code"] == "provider_not_configured"
        assert body["provider"] == "openai"

    def test_stream_returns_distinct_code(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        _force_provider_without_key(monkeypatch)
        conversation = _new_conversation(user)

        response = client.post(
            f"/chat/conversations/{conversation.id}/stream",
            json={"content": "hello"},
        )

        assert response.status_code == 503
        assert response.get_json()["code"] == "provider_not_configured"

    def test_nothing_is_persisted_when_blocked(self, client, make_user, login, monkeypatch):
        from app.models import Message

        user = make_user()
        login()
        _force_provider_without_key(monkeypatch)
        conversation = _new_conversation(user)

        client.post(
            f"/chat/conversations/{conversation.id}/stream",
            json={"content": "hello"},
        )

        assert Message.query.filter_by(conversation_id=conversation.id).count() == 0


class TestUnlockWithoutReload:
    def test_get_provider_injects_stored_key(self, app, make_user, monkeypatch):
        user = make_user()
        _force_provider_without_key(monkeypatch)

        assert get_provider(user=user).api_key == ""

        create_key(user, provider="openai", secret="sk-live")

        assert get_provider(user=user).api_key == "sk-live"

    def test_mock_provider_ignores_user_keys(self, app, make_user, monkeypatch):
        user = make_user()
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        create_key(user, provider="mock", secret="irrelevant")

        provider = get_provider(user=user)

        assert provider.name == "mock"
