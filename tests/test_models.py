"""Tests for the Phase 3 data models: Conversation, Message, and Prompt."""

from app.models import Conversation, Message, Prompt, User


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
    return User.query.filter_by(email=email).first()


class TestConversation:
    def test_conversation_belongs_to_user(self, client, db):
        user = _register(client)
        conversation = Conversation(user_id=user.id, title="My chat")
        db.session.add(conversation)
        db.session.commit()

        stored = Conversation.query.filter_by(user_id=user.id).first()
        assert stored.title == "My chat"
        assert stored.is_pinned is False
        assert stored.messages == []

    def test_conversation_default_title(self, client, db):
        user = _register(client)
        conversation = Conversation(user_id=user.id)
        db.session.add(conversation)
        db.session.commit()
        assert conversation.title == "New conversation"

    def test_deleting_conversation_cascades_messages(self, client, db):
        user = _register(client)
        conversation = Conversation(user_id=user.id, title="To delete")
        conversation.messages.append(Message(role="user", content="hello"))
        conversation.messages.append(Message(role="assistant", content="hi"))
        db.session.add(conversation)
        db.session.commit()

        db.session.delete(conversation)
        db.session.commit()
        assert Message.query.count() == 0

    def test_to_dict_includes_message_count(self, client, db):
        user = _register(client)
        conversation = Conversation(user_id=user.id, title="Counted")
        conversation.messages.append(Message(role="user", content="a"))
        db.session.add(conversation)
        db.session.commit()
        assert conversation.to_dict()["message_count"] == 1


class TestMessage:
    def test_message_serialization(self, client, db):
        user = _register(client)
        conversation = Conversation(user_id=user.id)
        message = Message(role="assistant", content="code here")
        conversation.messages.append(message)
        db.session.add(conversation)
        db.session.commit()

        payload = message.to_dict()
        assert payload["role"] == "assistant"
        assert payload["content"] == "code here"

    def test_message_stores_provider_model_and_tokens(self, client, db):
        user = _register(client)
        conversation = Conversation(user_id=user.id)
        message = Message(
            role="assistant",
            content="hi",
            provider="openai",
            model="gpt-4o-mini",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        conversation.messages.append(message)
        db.session.add(conversation)
        db.session.commit()

        stored = Message.query.filter_by(conversation_id=conversation.id).first()
        assert stored.provider == "openai"
        assert stored.model == "gpt-4o-mini"
        assert stored.total_tokens == 15
        payload = stored.to_dict()
        assert payload["provider"] == "openai"
        assert payload["model"] == "gpt-4o-mini"
        assert payload["token_usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }

    def test_message_stores_error_metadata(self, client, db):
        user = _register(client)
        conversation = Conversation(user_id=user.id)
        message = Message(
            role="assistant",
            content="",
            error="Provider timed out",
            error_code="timeout",
        )
        conversation.messages.append(message)
        db.session.add(conversation)
        db.session.commit()

        stored = Message.query.filter_by(conversation_id=conversation.id).first()
        assert stored.error == "Provider timed out"
        assert stored.error_code == "timeout"
        payload = stored.to_dict()
        assert payload["error"] == "Provider timed out"
        assert payload["error_code"] == "timeout"


class TestPrompt:
    def test_prompt_defaults(self, client, db):
        user = _register(client)
        prompt = Prompt(user_id=user.id, title="Refactor", content="Refactor this:")
        db.session.add(prompt)
        db.session.commit()
        assert prompt.category == "General"
        assert prompt.is_favorite is False

    def test_prompt_serialization(self, client, db):
        user = _register(client)
        prompt = Prompt(
            user_id=user.id,
            title="Docs",
            content="Document this:",
            category="Documentation",
            is_favorite=True,
        )
        db.session.add(prompt)
        db.session.commit()
        payload = prompt.to_dict()
        assert payload["category"] == "Documentation"
        assert payload["is_favorite"] is True
