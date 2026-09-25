"""Tests for chat image attachments (issue #49).

Covers upload validation, attaching an image to a user message, owner-scoped
serving, and provider payload construction with a text-only fallback.
"""

import io

from app.services.providers import MockProvider
from app.services.providers.anthropic import AnthropicProvider
from app.services.providers.base import prepare_messages
from app.services.providers.openai import OpenAIProvider

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 4


def _create_conversation(client):
    response = client.post("/chat/conversations", json={}, headers={"X-CSRFToken": "ignored"})
    assert response.status_code == 201
    return response.get_json()


def _upload(client, conversation_id, data=PNG_BYTES, filename="pic.png", content_type="image/png"):
    return client.post(
        f"/chat/conversations/{conversation_id}/attachments",
        data={"image": (io.BytesIO(data), filename, content_type)},
        content_type="multipart/form-data",
        headers={"X-CSRFToken": "ignored"},
    )


class TestUploadValidation:
    def test_accepts_png(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        response = _upload(client, conversation["id"])
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["content_type"] == "image/png"
        assert payload["size"] == len(PNG_BYTES)
        assert payload["url"] == f"/chat/attachments/{payload['id']}"

    def test_rejects_non_image_type(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        response = _upload(client, conversation["id"], filename="pic.gif", content_type="image/gif")
        assert response.status_code == 400

    def test_rejects_oversized_image(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        client.application.config["CHAT_IMAGE_MAX_BYTES"] = 8
        response = _upload(client, conversation["id"], data=b"x" * 32)
        assert response.status_code == 400
        assert "large" in response.get_json()["error"].lower()

    def test_requires_login(self, client):
        response = client.post("/chat/conversations/1/attachments")
        assert response.status_code == 302

    def test_upload_to_other_users_conversation_is_404(self, client, db, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        conversation = _create_conversation(client)
        # Switch to a different user.
        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")
        assert _upload(client, conversation["id"]).status_code == 404


class TestAttachAndServe:
    def test_attachment_is_linked_and_rendered(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        attachment = _upload(client, conversation["id"]).get_json()

        response = client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "What is wrong here?", "attachment_ids": [attachment["id"]]},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201

        stored = client.get(f"/chat/conversations/{conversation['id']}").get_json()
        user_message = stored["messages"][0]
        assert user_message["role"] == "user"
        assert user_message["attachments"][0]["id"] == attachment["id"]

    def test_image_only_message_is_allowed(self, client, db, make_user, login):
        make_user()
        login()
        conversation = _create_conversation(client)
        attachment = _upload(client, conversation["id"]).get_json()
        response = client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "", "attachment_ids": [attachment["id"]]},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201

    def test_cannot_link_attachment_from_another_conversation(self, client, db, make_user, login):
        make_user()
        login()
        first = _create_conversation(client)
        second = _create_conversation(client)
        attachment = _upload(client, first["id"]).get_json()
        response = client.post(
            f"/chat/conversations/{second['id']}/messages",
            json={"content": "hi", "attachment_ids": [attachment["id"]]},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 400

    def test_attachment_is_served_to_owner_only(self, client, db, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        conversation = _create_conversation(client)
        attachment = _upload(client, conversation["id"]).get_json()

        served = client.get(attachment["url"])
        assert served.status_code == 200
        assert served.mimetype == "image/png"
        assert served.data == PNG_BYTES

        make_user(username="stranger", email="stranger@example.com")
        login(email="stranger@example.com")
        assert client.get(attachment["url"]).status_code == 404


class TestProviderPayloads:
    def test_openai_builds_vision_content_parts(self):
        provider = OpenAIProvider(api_key="k")
        payload = provider._payload(
            [
                {
                    "role": "user",
                    "content": "What is this?",
                    "images": [{"content_type": "image/png", "data": "AAAA"}],
                }
            ],
            model=None,
            params=None,
            stream=False,
        )
        content = payload["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "What is this?"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"

    def test_anthropic_builds_image_blocks(self):
        provider = AnthropicProvider(api_key="k")
        payload = provider._payload(
            [
                {
                    "role": "user",
                    "content": "What is this?",
                    "images": [{"content_type": "image/jpeg", "data": "BBBB"}],
                }
            ],
            model=None,
            params=None,
            stream=False,
        )
        blocks = payload["messages"][0]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["media_type"] == "image/jpeg"
        assert blocks[0]["source"]["data"] == "BBBB"
        assert blocks[1] == {"type": "text", "text": "What is this?"}

    def test_text_only_provider_degrades_with_note(self):
        prepared = prepare_messages(
            [
                {
                    "role": "user",
                    "content": "look",
                    "images": [{"content_type": "image/png", "data": "x"}],
                }
            ],
            supports_vision=False,
        )
        assert "images" not in prepared[0]
        assert "omitted" in prepared[0]["content"]

    def test_mock_provider_reports_omitted_images(self):
        response = MockProvider().chat(
            [
                {
                    "role": "user",
                    "content": "describe",
                    "images": [{"content_type": "image/png", "data": "x"}],
                }
            ]
        )
        assert "omitted" in response.content
