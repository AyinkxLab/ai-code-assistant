"""Tests for duplicate project detection on import (#87).

Importing the same archive or GitHub repository twice into the same workspace is
detected and refused with ``409`` until the caller confirms; nothing is stored
unless confirmed. Detection is workspace-scoped.
"""

import base64
import io
import json
import zipfile
from typing import ClassVar

import pytest

from app.extensions import db
from app.models import GithubAccount, Project, Workspace
from app.models.project import SOURCE_ARCHIVE
from app.services.importing import find_duplicate_archive, find_duplicate_github

FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


class FakeResponse:
    def __init__(self, status_code=200, data=None, content_type="json"):
        self.status_code = status_code
        self._data = data
        self.headers = {"Content-Type": "application/json"} if content_type == "json" else {}
        self.text = ""
        self.content = (
            (data or "").encode()
            if content_type == "raw"
            else ((json.dumps(data) if data is not None else "").encode())
        )

    def json(self):
        return self._data


def _fake_github_session(script):
    ordered = sorted(script, key=lambda entry: len(entry[1]), reverse=True)

    class FakeSession:
        headers: ClassVar[dict] = {}

        def request(self, method, url, params=None, timeout=None, **kwargs):
            url_path = url.split("api.github.com", 1)[-1].split("?", 1)[0]
            for entry in ordered:
                if entry[0] in (method, "*") and (entry[1] == "*" or entry[1] in url_path):
                    return FakeResponse(entry[2], entry[3], entry[4])
            raise AssertionError(f"Unhandled request: {method} {url_path}")

        def get(self, url, params=None, timeout=None, **kwargs):
            return self.request("GET", url, params=params, timeout=timeout)

    return FakeSession()


def _zip_bytes(entries, date_time=FIXED_ZIP_TIME):
    """Build a deterministic ZIP payload so identical inputs hash identically."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(zipfile.ZipInfo(name, date_time=date_time), data)
    return buffer.getvalue()


def _upload_archive(client, workspace_id, bytes_, filename="project.zip", confirm=False):
    data = {"file": (io.BytesIO(bytes_), filename)}
    if confirm:
        data["confirm"] = "1"
    return client.post(
        f"/workspaces/api/workspaces/{workspace_id}/projects",
        data=data,
        content_type="multipart/form-data",
    )


def _github_entries(default_branch="main"):
    def contents(name):
        return {"content": base64.b64encode(f"print('{name}')\n".encode()).decode()}

    return [
        (
            "GET",
            "/repos/owner/demo",
            200,
            {"name": "demo", "default_branch": default_branch},
            "json",
        ),
        (
            "GET",
            f"/repos/owner/demo/git/trees/{default_branch}",
            200,
            {"tree": [{"path": "app.py", "type": "blob", "size": 20}], "truncated": False},
            "json",
        ),
        ("GET", "/repos/owner/demo/contents/app.py", 200, contents("app"), "json"),
    ]


def _connect_github(workspace):
    existing = GithubAccount.query.filter_by(user_id=workspace.user_id).first()
    if existing is None:
        account = GithubAccount(
            user_id=workspace.user_id, github_user_id=7, github_username="ghuser"
        )
        account.set_access_token("gho_test")
        db.session.add(account)
        db.session.commit()


def _import_github(client, workspace, monkeypatch, entries, confirm=False):
    _connect_github(workspace)
    monkeypatch.setattr(
        "app.services.github.requests.Session",
        lambda: _fake_github_session(entries),
    )
    payload = {"source": "github", "repo": "owner/demo"}
    if confirm:
        payload["confirm"] = True
    return client.post(
        f"/workspaces/api/workspaces/{workspace.id}/projects",
        json=payload,
    )


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Import workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


class TestArchiveDuplicates:
    def test_duplicate_archive_requires_confirmation(self, client, workspace):
        payload = _zip_bytes([("app.py", "print('hi')\n")])
        assert _upload_archive(client, workspace.id, payload).status_code == 201

        response = _upload_archive(client, workspace.id, payload)

        assert response.status_code == 409
        body = response.get_json()
        assert body["duplicate"] is True
        assert body["match"] == "archive"
        assert body["duplicate_of"]["source"] == SOURCE_ARCHIVE
        assert Project.query.count() == 1  # nothing was stored

    def test_confirmed_archive_import_creates_second_copy(self, client, workspace):
        payload = _zip_bytes([("app.py", "print('hi')\n")])
        _upload_archive(client, workspace.id, payload)

        response = _upload_archive(client, workspace.id, payload, confirm=True)

        assert response.status_code == 201
        assert Project.query.count() == 2
        hashes = {p.content_hash for p in Project.query.all()}
        assert len(hashes) == 1 and None not in hashes

    def test_different_archive_is_not_a_duplicate(self, client, workspace):
        _upload_archive(client, workspace.id, _zip_bytes([("app.py", "one\n")]))
        response = _upload_archive(client, workspace.id, _zip_bytes([("app.py", "two\n")]))

        assert response.status_code == 201
        assert Project.query.count() == 2

    def test_duplicate_detection_is_workspace_scoped(self, client, workspace):
        other = Workspace(user_id=workspace.user_id, name="Other workspace")
        db.session.add(other)
        db.session.commit()

        payload = _zip_bytes([("app.py", "print('hi')\n")])
        assert _upload_archive(client, workspace.id, payload).status_code == 201
        assert _upload_archive(client, other.id, payload).status_code == 201
        assert Project.query.count() == 2

    def test_find_duplicate_archive_helper(self, client, workspace):
        payload = _zip_bytes([("app.py", "print('hi')\n")])
        _upload_archive(client, workspace.id, payload)
        digest = Project.query.first().content_hash

        assert find_duplicate_archive(workspace.id, digest) is not None
        assert find_duplicate_archive(workspace.id, "0" * 64) is None


class TestGithubDuplicates:
    def test_duplicate_github_requires_confirmation(self, client, workspace, monkeypatch):
        entries = _github_entries()
        assert _import_github(client, workspace, monkeypatch, entries).status_code == 201

        response = _import_github(client, workspace, monkeypatch, entries)

        assert response.status_code == 409
        body = response.get_json()
        assert body["duplicate"] is True
        assert body["match"] == "github"
        assert body["duplicate_of"]["source_url"] == "owner/demo"
        assert Project.query.count() == 1

    def test_confirmed_github_import_creates_second_copy(self, client, workspace, monkeypatch):
        entries = _github_entries()
        _import_github(client, workspace, monkeypatch, entries)

        response = _import_github(client, workspace, monkeypatch, entries, confirm=True)

        assert response.status_code == 201
        assert Project.query.count() == 2

    def test_different_default_branch_is_not_duplicate(self, client, workspace, monkeypatch):
        assert (
            _import_github(client, workspace, monkeypatch, _github_entries("main")).status_code
            == 201
        )
        response = _import_github(client, workspace, monkeypatch, _github_entries("develop"))

        assert response.status_code == 201
        assert Project.query.count() == 2
        branches = {p.default_branch for p in Project.query.all()}
        assert branches == {"main", "develop"}

    def test_find_duplicate_github_helper_is_case_insensitive(self, client, workspace, monkeypatch):
        _import_github(client, workspace, monkeypatch, _github_entries("main"))

        assert find_duplicate_github(workspace.id, "Owner/Demo", "main") is not None
        assert find_duplicate_github(workspace.id, "owner/demo", "other") is None
