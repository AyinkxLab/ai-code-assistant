"""Additional chat coverage: attachments, shares, and export (issue #20)."""

import io
import json

from app.models import ConversationShare, User


def _register(client, username="covuser", email="covuser@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _create_conversation(client, title="Coverage chat"):
    response = client.post(
        "/chat/conversations", json={"title": title}, headers={"X-CSRFToken": "ignored"}
    )
    assert response.status_code == 201
    return response.get_json()


def _conversation(client, title="Coverage chat"):
    return _create_conversation(client, title)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class TestAttachmentUpload:
    def _upload(self, client, conversation_id, *, data, config=None, app=None):
        if app is not None and config is not None:
            for key, value in config.items():
                app.config[key] = value
        return client.post(
            f"/chat/conversations/{conversation_id}/attachments",
            data=data,
            headers={"X-CSRFToken": "ignored"},
        )

    def test_missing_file_is_rejected(self, client, db):
        _register(client)
        conversation = _conversation(client)

        response = self._upload(client, conversation["id"], data={})

        assert response.status_code == 400

    def test_unsupported_type_is_rejected(self, client, db):
        _register(client)
        conversation = _conversation(client)

        response = self._upload(
            client,
            conversation["id"],
            data={"image": (io.BytesIO(b"hello"), "notes.txt", "text/plain")},
        )

        assert response.status_code == 400

    def test_empty_file_is_rejected(self, client, db):
        _register(client)
        conversation = _conversation(client)

        response = self._upload(
            client,
            conversation["id"],
            data={"image": (io.BytesIO(b""), "empty.png", "image/png")},
        )

        assert response.status_code == 400

    def test_oversized_file_is_rejected(self, client, db, app):
        _register(client)
        conversation = _conversation(client)

        response = self._upload(
            client,
            conversation["id"],
            data={"image": (io.BytesIO(PNG), "big.png", "image/png")},
            app=app,
            config={"CHAT_IMAGE_MAX_BYTES": 4},
        )

        assert response.status_code == 400

    def test_valid_upload_round_trips_and_is_owner_scoped(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        conversation = _conversation(client)

        created = self._upload(
            client,
            conversation["id"],
            data={"image": (io.BytesIO(PNG), "photo.png", "image/png")},
        )
        assert created.status_code == 201
        attachment_id = created.get_json()["id"]

        served = client.get(f"/chat/attachments/{attachment_id}")
        assert served.status_code == 200
        assert served.data == PNG

        client.post("/auth/logout")
        _register(client, username="other", email="other@example.com")
        assert client.get(f"/chat/attachments/{attachment_id}").status_code == 404


class TestSharesAndExport:
    def test_list_shares_returns_recipients(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        conversation = _conversation(client)
        recipient = User(username="recipient", email="recipient@example.com")
        recipient.set_password("supersecret123")
        db.session.add(recipient)
        db.session.commit()
        db.session.add(
            ConversationShare(
                conversation_id=conversation["id"],
                user_id=recipient.id,
                shared_by_id=recipient.id,
            )
        )
        db.session.commit()

        response = client.get(f"/chat/conversations/{conversation['id']}/shares")

        assert response.status_code == 200
        assert [share["user_id"] for share in response.get_json()] == [recipient.id]

    def test_share_without_username_is_rejected(self, client, db):
        _register(client)
        conversation = _conversation(client)

        response = client.post(
            f"/chat/conversations/{conversation['id']}/shares",
            json={},
            headers={"X-CSRFToken": "ignored"},
        )

        assert response.status_code == 400

    def test_export_returns_json_document(self, client, db):
        _register(client)
        conversation = _conversation(client, title="Export me")
        client.post(
            f"/chat/conversations/{conversation['id']}/messages",
            json={"content": "hi"},
            headers={"X-CSRFToken": "ignored"},
        )

        response = client.get(f"/chat/conversations/{conversation['id']}/export")

        assert response.status_code == 200
        payload = json.loads(response.get_data(as_text=True))
        assert payload["conversation"]["title"] == "Export me"
        assert len(payload["messages"]) >= 2
