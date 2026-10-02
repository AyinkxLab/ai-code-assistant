"""Persisted file uploads (issue #42).

Covers upload, listing, retrieval, download, conversation attachment, and
re-analysis, plus the boundary cases: unsupported types, empty/oversized
uploads, and owner-only access (a second user gets 404, never the file).
"""

import io

import pytest

from app.models import StoredFile


@pytest.fixture()
def upload_dir(app, tmp_path):
    """Point UPLOAD_FOLDER at an isolated temp directory per test."""
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    return tmp_path


def _login_new_user(client, make_user, username, email):
    make_user(username=username, email=email)
    client.post(
        "/auth/login",
        data={"email": email, "password": "supersecret123"},
        follow_redirects=True,
    )


def _upload(client, filename="hello.py", content=b"print('hi')\n", **form):
    return client.post(
        "/files",
        data={"file": (io.BytesIO(content), filename), **form},
        content_type="multipart/form-data",
    )


def test_upload_persists_file_on_disk_and_in_db(app, client, make_user, upload_dir):
    _login_new_user(client, make_user, "uploader", "uploader@example.com")

    response = _upload(client)
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["original_name"] == "hello.py"
    assert payload["size"] == len(b"print('hi')\n")
    assert payload["download_url"] == f"/files/{payload['id']}/download"

    with app.app_context():
        record = StoredFile.query.one()
        assert record.original_name == "hello.py"
        # The stored name is random and never the user-supplied name.
        assert record.stored_name != "hello.py"
        assert (upload_dir / record.stored_name).read_bytes() == b"print('hi')\n"


def test_upload_rejects_unsupported_extension(client, make_user, upload_dir):
    _login_new_user(client, make_user, "badtype", "badtype@example.com")
    response = _upload(client, filename="payload.exe", content=b"MZ")
    assert response.status_code == 400
    assert "Unsupported" in response.get_json()["error"]


def test_upload_rejects_empty_file(client, make_user, upload_dir):
    _login_new_user(client, make_user, "empty", "empty@example.com")
    response = _upload(client, content=b"")
    assert response.status_code == 400


def test_upload_rejects_oversized_file(app, client, make_user, upload_dir):
    app.config["UPLOAD_MAX_BYTES"] = 10
    _login_new_user(client, make_user, "big", "big@example.com")
    response = _upload(client, content=b"x" * 11)
    assert response.status_code == 400


def test_list_returns_only_own_files(client, make_user, login, upload_dir):
    _login_new_user(client, make_user, "owner", "owner@example.com")
    _upload(client, filename="mine.py")

    make_user(username="other", email="other@example.com")
    login(email="other@example.com")
    _upload(client, filename="theirs.py")

    login(email="owner@example.com")
    names = [item["original_name"] for item in client.get("/files").get_json()]
    assert names == ["mine.py"]


def test_get_and_download_are_owner_only(client, make_user, login, upload_dir):
    _login_new_user(client, make_user, "alice", "alice@example.com")
    file_id = _upload(client, filename="secret.py", content=b"token = 1\n").get_json()["id"]

    # Owner can read metadata and download the bytes.
    assert client.get(f"/files/{file_id}").status_code == 200
    download = client.get(f"/files/{file_id}/download")
    assert download.status_code == 200
    assert download.data == b"token = 1\n"

    # A different user sees a 404, never the file.
    make_user(username="bob", email="bob@example.com")
    login(email="bob@example.com")
    assert client.get(f"/files/{file_id}").status_code == 404
    assert client.get(f"/files/{file_id}/download").status_code == 404
    assert client.post(f"/files/{file_id}/attach", json={"conversation_id": 1}).status_code == 404
    assert client.post(f"/files/{file_id}/analyze", json={}).status_code == 404


def test_attach_to_own_conversation_but_not_anothers(app, client, make_user, login, upload_dir):
    from app.models import Conversation

    _login_new_user(client, make_user, "carol", "carol@example.com")
    file_id = _upload(client).get_json()["id"]

    with app.app_context():
        own = Conversation(user_id=1, title="Own chat")
        from app.extensions import db

        db.session.add(own)
        db.session.commit()
        own_id = own.id

    attached = client.post(f"/files/{file_id}/attach", json={"conversation_id": own_id})
    assert attached.status_code == 200
    assert attached.get_json()["conversation_id"] == own_id

    # Detach.
    detached = client.post(f"/files/{file_id}/attach", json={"conversation_id": None})
    assert detached.get_json()["conversation_id"] is None

    # Another user's conversation is not a valid target.
    make_user(username="dave", email="dave@example.com")
    login(email="dave@example.com")
    with app.app_context():
        daves = Conversation(user_id=2, title="Dave chat")
        from app.extensions import db

        db.session.add(daves)
        db.session.commit()
        daves_id = daves.id

    login(email="carol@example.com")
    forbidden = client.post(f"/files/{file_id}/attach", json={"conversation_id": daves_id})
    assert forbidden.status_code == 404


def test_analyze_reuses_stored_bytes(app, client, make_user, upload_dir):
    _login_new_user(client, make_user, "reader", "reader@example.com")
    file_id = _upload(client, content=b"def add(a, b):\n    return a + b\n").get_json()["id"]

    response = client.post(f"/files/{file_id}/analyze", json={"action": "explain"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["file_id"] == file_id
    assert body["action"] == "explain"
    assert isinstance(body["result"], str) and body["result"]
