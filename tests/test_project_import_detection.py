"""Tests for Stellar/Soroban detection during project import.

Verifies that import responses carry detection metadata, that
``stellar.network.detected`` is emitted for Stellar projects, and that the
per-project Stellar metadata endpoint is owner-scoped.
"""

import base64
import io
import zipfile
from typing import ClassVar

import pytest

from app.extensions import db
from app.models import GithubAccount, Project, Workspace
from app.services.events import get_dispatcher


class FakeResponse:
    def __init__(self, status_code=200, data=None, content_type="json"):
        self.status_code = status_code
        self._data = data
        self.headers = {"Content-Type": "application/json"} if content_type == "json" else {}
        self.content = (
            (data or "").encode()
            if content_type == "raw"
            else ((__import__("json").dumps(data) if data is not None else "").encode())
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


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Import workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _github_import(client, workspace, entries):
    account = GithubAccount(user_id=workspace.user_id, github_user_id=7, github_username="ghuser")
    account.set_access_token("gho_test")
    db.session.add(account)
    db.session.commit()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "app.services.github.requests.Session",
        lambda: _fake_github_session(entries),
    )
    try:
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={"source": "github", "repo": "owner/demo"},
        )
    finally:
        monkeypatch.undo()
    return response


def _contents(name):
    return {"content": base64.b64encode(name.encode()).decode()}


def _entries_for(tree):
    blob_tree = [{"path": path, "type": "blob", "size": len(content)} for path, content in tree]
    return [
        ("GET", "/repos/owner/demo", 200, {"name": "demo", "default_branch": "main"}, "json"),
        (
            "GET",
            "/repos/owner/demo/git/trees/main",
            200,
            {"tree": blob_tree, "truncated": False},
            "json",
        ),
    ] + [
        (
            "GET",
            f"/repos/owner/demo/contents/{path}",
            200,
            _contents(content),
            "json",
        )
        for path, content in tree
    ]


class TestImportDetection:
    def test_github_import_attaches_stellar_metadata(self, client, workspace):
        entries = _entries_for(
            [
                ("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
                ("src/lib.rs", "#![no_std]\n#[contractimpl]\npub struct C {}\n"),
            ]
        )
        response = _github_import(client, workspace, entries)
        assert response.status_code == 201
        data = response.get_json()
        assert data["stellar"]["is_stellar"] is True
        assert data["stellar"]["confidence"] == "likely"
        assert data["stellar"]["is_soroban"] is True
        assert data["stellar"]["relevant_files"]

    def test_github_import_non_stellar(self, client, workspace):
        entries = _entries_for([("app.py", "print('hi')\n"), ("README.md", "# app")])
        response = _github_import(client, workspace, entries)
        assert response.status_code == 201
        data = response.get_json()
        assert data["stellar"]["is_stellar"] is False
        assert data["stellar"]["confidence"] == "none"

    def test_archive_import_attaches_stellar_metadata(self, client, workspace):
        payload = _zip_bytes(
            [
                ("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
                ("src/lib.rs", "#![no_std]\n#[contractimpl]\npub struct C {}\n"),
            ]
        )
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            data={"file": (io.BytesIO(payload), "project.zip")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 201
        data = response.get_json()
        assert data["stellar"]["is_stellar"] is True
        assert data["name"]  # project fields remain at the top level

    def test_stellar_event_emitted_for_stellar_import(self, client, workspace):
        received = {}

        def handler(event):
            received.update(event.data)

        get_dispatcher().subscribe("stellar.network.detected", handler)
        entries = _entries_for([("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n")])
        response = _github_import(client, workspace, entries)
        assert response.status_code == 201
        assert received.get("confidence") == "likely"

    def test_no_stellar_event_for_plain_project(self, client, workspace):
        emitted = []

        def handler(event):
            emitted.append(event.event_type)

        get_dispatcher().subscribe("stellar.network.detected", handler)
        entries = _entries_for([("app.py", "print('hi')\n")])
        response = _github_import(client, workspace, entries)
        assert response.status_code == 201
        assert emitted == []


class TestProjectStellarApi:
    def test_stellar_metadata_endpoint(self, client, workspace):

        project = Project(
            workspace_id=workspace.id,
            user_id=workspace.user_id,
            name="Demo",
            source="archive",
            status="ready",
        )
        db.session.add(project)
        db.session.commit()
        from app.models.project_file import ProjectFile

        db.session.add(
            ProjectFile(
                project_id=project.id,
                path="Cargo.toml",
                size=20,
                is_binary=False,
                language="toml",
                content="[dependencies]\nsoroban-sdk = '21.0.0'\n",
            )
        )
        db.session.commit()

        response = client.get(f"/workspaces/api/projects/{project.id}/stellar")
        assert response.status_code == 200
        data = response.get_json()
        assert data["is_stellar"] is True
        assert data["confidence"] == "likely"
        assert "network" in data

    def test_other_user_cannot_read_stellar_metadata(self, app, client, make_user, login, db):
        from app.models import User

        other = User(username="other", email="other@example.com")
        other.set_password("supersecret123")
        db.session.add(other)
        db.session.commit()
        ws = Workspace(user_id=other.id, name="Other workspace")
        db.session.add(ws)
        db.session.commit()
        project = Project(workspace_id=ws.id, user_id=other.id, name="P", source="archive")
        db.session.add(project)
        db.session.commit()

        make_user()
        login()
        response = client.get(f"/workspaces/api/projects/{project.id}/stellar")
        assert response.status_code == 404
