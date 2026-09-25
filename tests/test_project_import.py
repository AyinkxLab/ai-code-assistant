"""Tests for project import: archive extraction security and GitHub import."""

import base64
import io
import zipfile
from typing import ClassVar

import pytest

from app.extensions import db
from app.models import GithubAccount, Project, ProjectFile, Workspace
from app.models.project import SOURCE_ARCHIVE, SOURCE_GITHUB, STATUS_READY
from app.services.importing import (
    ProjectImportError,
    build_manifest_rows,
    detect_language,
    sanitize_member_path,
    should_skip,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, content_type="json"):
        self.status_code = status_code
        self._data = data
        self.headers = {"Content-Type": "application/json"} if content_type == "json" else {}
        self.text = ""
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


def _upload_archive(client, workspace_id, bytes_, filename="project.zip"):
    return client.post(
        f"/workspaces/api/workspaces/{workspace_id}/projects",
        data={"file": (io.BytesIO(bytes_), filename)},
        content_type="multipart/form-data",
    )


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Import workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


class TestSanitizeHelpers:
    def test_sanitize_normalizes_backslashes(self):
        assert sanitize_member_path("a\\b\\c.py") == "a/b/c.py"

    def test_sanitize_rejects_absolute(self):
        with pytest.raises(ProjectImportError):
            sanitize_member_path("/etc/passwd")

    def test_sanitize_rejects_drive_letter(self):
        with pytest.raises(ProjectImportError):
            sanitize_member_path("C:\\Windows\\system32")

    def test_sanitize_rejects_traversal(self):
        with pytest.raises(ProjectImportError):
            sanitize_member_path("../../etc/passwd")

    def test_should_skip_secret_and_vendor(self, app):
        assert should_skip(".env")
        assert should_skip("config/.env.production")
        assert should_skip("server.pem")
        assert should_skip("node_modules/pkg/index.js")
        assert should_skip("src/__pycache__/x.py")
        assert not should_skip("app.py")

    def test_detect_language(self):
        assert detect_language("app.py") == "Python"
        assert detect_language("main.js") == "JavaScript"
        assert detect_language("unknown.xyz") is None


class TestArchiveImport:
    def test_import_zip(self, client, workspace):
        payload = _zip_bytes([("README.md", "# Demo"), ("app.py", "print('hi')\n")])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 201
        project = Project.query.first()
        assert project.status == STATUS_READY
        assert project.source == SOURCE_ARCHIVE
        assert project.file_count == 2
        app_py = ProjectFile.query.filter_by(path="app.py").first()
        assert app_py.content == "print('hi')\n"
        assert app_py.language == "Python"
        assert app_py.is_binary is False

    def test_import_tar_gz(self, client, workspace):
        buffer = io.BytesIO()
        import tarfile

        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            data = b"hello tar\n"
            info = tarfile.TarInfo("hello.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        buffer.seek(0)
        response = _upload_archive(client, workspace.id, buffer.getvalue(), "project.tar.gz")
        assert response.status_code == 201
        assert Project.query.first().file_count == 1

    def test_unsupported_extension(self, client, workspace):
        response = _upload_archive(client, workspace.id, b"not an archive", "notes.docx")
        assert response.status_code == 400
        assert "Unsupported archive type" in response.get_json()["error"]
        assert Project.query.count() == 0

    def test_invalid_zip_rejected(self, client, workspace):
        response = _upload_archive(client, workspace.id, b"this is not a zip file", "bad.zip")
        assert response.status_code == 400
        assert Project.query.count() == 0

    def test_zip_slip_rejected(self, client, workspace):
        payload = _zip_bytes([("../../../evil.py", "malicious")])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 400
        assert "escapes" in response.get_json()["error"]
        assert Project.query.count() == 0

    def test_absolute_path_rejected(self, client, workspace):
        payload = _zip_bytes([("C:/Users/evil/pwn.py", "malicious")])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 400
        assert Project.query.count() == 0

    def test_symlink_entry_skipped(self, client, workspace):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            link = zipfile.ZipInfo("link.py")
            link.external_attr = (0o120000 << 16) | 0o644
            archive.writestr(link, "target.py")
            archive.writestr("real.py", "ok\n")
        response = _upload_archive(client, workspace.id, buffer.getvalue())
        assert response.status_code == 201
        project = Project.query.first()
        assert project.file_count == 1
        assert ProjectFile.query.filter_by(path="link.py").first() is None

    def test_secret_and_vendor_files_skipped(self, client, workspace):
        payload = _zip_bytes(
            [
                (".env", "SECRET=1"),
                ("node_modules/lib/index.js", "x"),
                ("app.py", "ok"),
            ]
        )
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 201
        paths = {f.path for f in ProjectFile.query.all()}
        assert paths == {"app.py"}

    def test_binary_file_has_no_content(self, client, workspace):
        payload = _zip_bytes([("image.bin", b"\x89PNG\r\n\x00\x1a" + b"\x00" * 100)])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 201
        binary = ProjectFile.query.filter_by(path="image.bin").first()
        assert binary.is_binary is True
        assert binary.content is None

    def test_oversized_text_file_not_stored(self, client, workspace, app):
        app.config["PROJECT_MAX_FILE_CHARS"] = 100
        payload = _zip_bytes([("big.py", "x" * 500)])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 201
        big = ProjectFile.query.filter_by(path="big.py").first()
        assert big is not None
        assert big.is_binary is False
        assert big.content is None

    def test_archive_too_large_rejected(self, client, workspace, app):
        import os

        app.config["PROJECT_MAX_ARCHIVE_BYTES"] = 100
        payload = _zip_bytes([("a.bin", os.urandom(500))])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 400
        assert "maximum allowed upload size" in response.get_json()["error"]
        assert Project.query.count() == 0

    def test_too_many_files_rejected(self, client, workspace, app):
        app.config["PROJECT_MAX_FILE_COUNT"] = 2
        payload = _zip_bytes([("a.py", "1"), ("b.py", "2"), ("c.py", "3")])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 400
        assert "too many files" in response.get_json()["error"]
        assert Project.query.count() == 0

    def test_empty_archive_rejected(self, client, workspace):
        payload = _zip_bytes([])
        response = _upload_archive(client, workspace.id, payload)
        assert response.status_code == 400
        assert Project.query.count() == 0


class TestGithubImport:
    def _import(self, client, workspace, monkeypatch, entries):
        account = GithubAccount(
            user_id=workspace.user_id, github_user_id=7, github_username="ghuser"
        )
        account.set_access_token("gho_test")
        db.session.add(account)
        db.session.commit()

        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _fake_github_session(entries),
        )
        return client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={"source": "github", "repo": "owner/demo"},
        )

    def test_import_github_repo(self, client, workspace, monkeypatch):
        def contents(name):
            return {"content": base64.b64encode(f"print('{name}')\n".encode()).decode()}

        entries = [
            ("GET", "/repos/owner/demo", 200, {"name": "demo", "default_branch": "main"}, "json"),
            (
                "GET",
                "/repos/owner/demo/git/trees/main",
                200,
                {
                    "tree": [
                        {"path": "app.py", "type": "blob", "size": 20},
                        {"path": "README.md", "type": "blob", "size": 15},
                    ],
                    "truncated": False,
                },
                "json",
            ),
            ("GET", "/repos/owner/demo/contents/app.py", 200, contents("app"), "json"),
            ("GET", "/repos/owner/demo/contents/README.md", 200, contents("readme"), "json"),
        ]
        response = self._import(client, workspace, monkeypatch, entries)
        assert response.status_code == 201
        project = Project.query.first()
        assert project.source == SOURCE_GITHUB
        assert project.source_url == "owner/demo"
        assert project.status == STATUS_READY
        assert project.file_count == 2
        app_file = ProjectFile.query.filter_by(path="app.py").first()
        assert app_file.content == "print('app')\n"
        assert app_file.is_binary is False

    def test_import_github_not_found(self, client, workspace, monkeypatch):
        from app.services.github import GitHubNotFoundError

        def raise_not_found(*args, **kwargs):
            raise GitHubNotFoundError("The requested GitHub resource was not found.")

        account = GithubAccount(
            user_id=workspace.user_id, github_user_id=7, github_username="ghuser"
        )
        account.set_access_token("gho_test")
        db.session.add(account)
        db.session.commit()

        monkeypatch.setattr("app.services.github.GitHubClient.get_repository", raise_not_found)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={"source": "github", "repo": "owner/missing"},
        )
        assert response.status_code == 502
        assert Project.query.count() == 0

    def test_invalid_repo_name_rejected(self, client, workspace):
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={"source": "github", "repo": "not a valid name"},
        )
        assert response.status_code == 400
        assert Project.query.count() == 0

    def test_missing_repo_rejected(self, client, workspace):
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={"source": "github"},
        )
        assert response.status_code == 400

    def test_imported_project_can_be_deleted(self, client, workspace):
        payload = _zip_bytes([("app.py", "x")])
        response = _upload_archive(client, workspace.id, payload)
        project_id = response.get_json()["id"]
        assert client.delete(f"/workspaces/api/projects/{project_id}").status_code == 200
        assert Project.query.count() == 0


class TestManifestImport:
    """Drag-and-drop single file / folder manifest import (#91)."""

    def test_manifest_rows_validate_and_classify(self, app):
        rows = build_manifest_rows(
            [
                {"path": "src/app.py", "content": "print('hi')\n"},
                {"path": "README.md", "content": "# Demo"},
            ]
        )
        by_path = {row["path"]: row for row in rows}
        assert by_path["src/app.py"]["language"] == "Python"
        assert by_path["src/app.py"]["content"] == "print('hi')\n"
        assert by_path["README.md"]["language"] == "Markdown"

    def test_manifest_skips_secret_and_vendor_paths(self, app):
        rows = build_manifest_rows(
            [
                {"path": ".env", "content": "SECRET=1"},
                {"path": "node_modules/x/index.js", "content": "x"},
                {"path": "app.py", "content": "ok"},
            ]
        )
        assert [row["path"] for row in rows] == ["app.py"]

    def test_manifest_rejects_traversal(self, app):
        with pytest.raises(ProjectImportError):
            build_manifest_rows([{"path": "../etc/passwd", "content": "x"}])

    def test_manifest_rejects_absolute_path(self, app):
        with pytest.raises(ProjectImportError):
            build_manifest_rows([{"path": "/etc/passwd", "content": "x"}])

    def test_manifest_requires_files(self, app):
        with pytest.raises(ProjectImportError):
            build_manifest_rows([])

    def test_import_folder_manifest(self, client, workspace):
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={
                "source": "manifest",
                "name": "dropped-folder",
                "files": [
                    {"path": "pkg/a.py", "content": "a = 1\n"},
                    {"path": "pkg/b.js", "content": "const b = 2;\n"},
                ],
            },
        )
        assert response.status_code == 201
        project = Project.query.first()
        assert project.name == "dropped-folder"
        assert project.status == STATUS_READY
        assert project.file_count == 2
        assert ProjectFile.query.filter_by(path="pkg/a.py").first().language == "Python"

    def test_single_file_creates_minimal_project(self, client, workspace):
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={
                "source": "manifest",
                "files": [{"path": "hello.py", "content": "print('hello')\n"}],
            },
        )
        assert response.status_code == 201
        project = Project.query.first()
        # No folder/name provided -> named after the single dropped file.
        assert project.name == "hello"
        assert project.file_count == 1

    def test_manifest_traversal_rejected_by_route(self, client, workspace):
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={
                "source": "manifest",
                "files": [{"path": "../../etc/passwd", "content": "x"}],
            },
        )
        assert response.status_code == 400
        assert Project.query.count() == 0
