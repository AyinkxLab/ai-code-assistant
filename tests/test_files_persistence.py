"""Persisted uploads (issue #42).

Covers upload, retrieval, download, and owner-only access for the ``files``
table and the ``/tools/files`` endpoints. The stored bytes live on disk under
``UPLOAD_FOLDER``; only metadata lives in the database.
"""

import io
from pathlib import Path

from flask import current_app

from app.extensions import db
from app.models import Conversation, UploadedFile, User, Workspace

SAMPLE = b"print('hello')\n"


def _workspace(owner, name="Upload workspace"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _upload(client, workspace_id, filename="sample.py", content=SAMPLE):
    return client.post(
        "/tools/files",
        data={
            "workspace_id": str(workspace_id),
            "file": (io.BytesIO(content), filename),
        },
        content_type="multipart/form-data",
    )


class TestUpload:
    def test_upload_persists_metadata_and_writes_the_file(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        workspace = _workspace(owner)

        response = _upload(client, workspace.id)

        assert response.status_code == 201
        data = response.get_json()
        assert data["original_name"] == "sample.py"
        assert data["workspace_id"] == workspace.id
        assert data["size"] == len(SAMPLE)
        assert data["content_type"] == "text/x-python"

        row = UploadedFile.query.filter_by(id=data["id"]).one()
        assert row.user_id == owner.id
        # Stored name is random, not the original, and the bytes are on disk.
        assert row.stored_name != row.original_name
        stored = Path(current_app.config["UPLOAD_FOLDER"]) / row.stored_name
        assert stored.read_bytes() == SAMPLE

    def test_upload_rejects_unsupported_type(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        workspace = _workspace(owner)

        response = _upload(client, workspace.id, filename="malware.exe")
        assert response.status_code == 400
        assert UploadedFile.query.count() == 0

    def test_upload_requires_an_owned_workspace(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        other_workspace = _workspace(other, name="Other workspace")
        login(email="owner@example.com")

        response = _upload(client, other_workspace.id)
        assert response.status_code == 400
        assert UploadedFile.query.count() == 0


class TestRetrieval:
    def test_list_get_and_download(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        workspace = _workspace(owner)
        file_id = _upload(client, workspace.id).get_json()["id"]

        listed = client.get(f"/tools/files?workspace_id={workspace.id}").get_json()
        assert [item["id"] for item in listed] == [file_id]

        fetched = client.get(f"/tools/files/{file_id}")
        assert fetched.status_code == 200
        assert fetched.get_json()["original_name"] == "sample.py"

        download = client.get(f"/tools/files/{file_id}/download")
        assert download.status_code == 200
        assert download.data == SAMPLE


class TestOwnerOnlyAccess:
    def test_other_users_cannot_read_or_download(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace(owner)
        file_id = _upload(client, workspace.id).get_json()["id"]

        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")

        assert client.get(f"/tools/files/{file_id}").status_code == 404
        assert client.get(f"/tools/files/{file_id}/download").status_code == 404
        assert client.post(
            f"/tools/files/{file_id}/attach", json={"conversation_id": 1}
        ).status_code == 404
        assert client.get("/tools/files").get_json() == []


class TestAttachAndReanalyze:
    def test_attach_associates_a_conversation(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        workspace = _workspace(owner)
        conversation = Conversation(user_id=owner.id, title="Attach target")
        db.session.add(conversation)
        db.session.commit()
        file_id = _upload(client, workspace.id).get_json()["id"]

        response = client.post(
            f"/tools/files/{file_id}/attach", json={"conversation_id": conversation.id}
        )
        assert response.status_code == 200
        assert response.get_json()["conversation_id"] == conversation.id
        assert (
            UploadedFile.query.filter_by(id=file_id).one().conversation_id == conversation.id
        )

    def test_reanalyze_reuses_the_stored_file(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        workspace = _workspace(owner)
        file_id = _upload(client, workspace.id).get_json()["id"]

        response = client.post(
            f"/tools/files/{file_id}/reanalyze", json={"action": "explain"}
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["action"] == "explain"
        assert body["result"]
        assert body["file"]["original_name"] == "sample.py"
