"""Tests for the Markdown conversation export endpoint (issue #46)."""

from datetime import UTC, datetime

from app.extensions import db as database
from app.models import Conversation, Message


def _register(client, username="mduser", email="mduser@example.com"):
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


def _export(client, conversation_id):
    return client.get(f"/chat/conversations/{conversation_id}/export.md")


def _add_messages(conversation_id, *messages):
    """Append ``(role, content)`` pairs to a conversation and commit."""
    conversation = database.session.get(Conversation, conversation_id)
    for role, content in messages:
        conversation.messages.append(Message(role=role, content=content))
    database.session.commit()
    return conversation


class TestMarkdownExportAuth:
    def test_requires_login(self, client):
        response = _export(client, 1)
        assert response.status_code == 302

    def test_other_users_conversation_is_not_found(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Private")
        _add_messages(conversation["id"], ("user", "top secret"))

        _logout(client)
        _register(client, username="intruder", email="intruder@example.com")

        response = _export(client, conversation["id"])
        # A 404 (not 403) avoids leaking that the conversation exists.
        assert response.status_code == 404


class TestMarkdownExportHeaders:
    def test_content_type_is_markdown_utf8(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Headers")

        response = _export(client, conversation["id"])
        assert response.status_code == 200
        assert response.mimetype == "text/markdown"
        assert response.headers["Content-Type"] == "text/markdown; charset=utf-8"

    def test_download_filename_is_sensible(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Headers")

        response = _export(client, conversation["id"])
        assert (
            response.headers["Content-Disposition"]
            == f'attachment; filename="conversation-{conversation["id"]}.md"'
        )


class TestMarkdownExportStructure:
    def test_messages_are_ordered_with_role_headings(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Ordering")
        _add_messages(
            conversation["id"],
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "second question"),
            ("assistant", "second answer"),
        )

        body = _export(client, conversation["id"]).get_data(as_text=True)

        assert body.startswith("# Ordering")
        assert "## User" in body
        assert "## Assistant" in body
        # Every message appears strictly after the previous one.
        markers = [
            body.index("first question"),
            body.index("first answer"),
            body.index("second question"),
            body.index("second answer"),
        ]
        assert markers == sorted(markers)

    def test_order_falls_back_to_id_on_tied_timestamps(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Ties")
        conversation_row = database.session.get(Conversation, conversation["id"])
        same_instant = datetime.now(UTC)
        conversation_row.messages.append(
            Message(role="user", content="earlier", created_at=same_instant)
        )
        conversation_row.messages.append(
            Message(role="assistant", content="later", created_at=same_instant)
        )
        database.session.commit()

        body = _export(client, conversation["id"]).get_data(as_text=True)
        assert body.index("earlier") < body.index("later")

    def test_code_blocks_are_preserved_as_fences(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Code")
        code = "Try this:\n\n```python\nprint('hi')\n```\n\nThat is all."
        _add_messages(conversation["id"], ("assistant", code))

        body = _export(client, conversation["id"]).get_data(as_text=True)

        assert "```python" in body
        assert "print('hi')" in body
        assert body.count("```") == 2
        # The fence is on its own line, not glued to the surrounding text.
        assert "\n```python\n" in body

    def test_content_is_not_double_escaped(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Verbatim")
        content = "Use `**literal**`, keep &amp; and <div>tag</div>"
        _add_messages(conversation["id"], ("user", content))

        body = _export(client, conversation["id"]).get_data(as_text=True)

        assert "**literal**" in body
        assert "&amp;" in body
        assert "&amp;amp;" not in body
        assert "<div>tag</div>" in body

    def test_empty_conversation_is_still_valid_markdown(self, client, db):
        _register(client)
        conversation = _create_conversation(client, title="Empty")

        response = _export(client, conversation["id"])
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert body.startswith("# Empty")
        assert "no messages" in body.lower()
