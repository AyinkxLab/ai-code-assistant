"""Tests for project snapshot export (#107).

Covers: streaming zip export of stored files, placeholder stubs for
binary/oversized files, the manifest, owner-only access, the indexing-status
block, activity recording, and the per-user rate limit.
"""

import io
import json
import zipfile

from app.extensions import db
from app.models import Project, ProjectFile, Workspace
from app.models.activity_event import EVENT_PROJECT_EXPORTED
from app.models.project import SOURCE_ARCHIVE, STATUS_INDEXING, STATUS_READY


def _setup(make_user, login, *, username="exporter", email="exporter@example.com"):
    user = make_user(username=username, email=email)
    login(email=email)
    workspace = Workspace(user_id=user.id, name="Export workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Demo Project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
        file_count=3,
        total_size_bytes=24,
        indexed_at=db.func.now(),
    )
    db.session.add(project)
    db.session.commit()
    db.session.add_all(
        [
            ProjectFile(
                project_id=project.id,
                path="app.py",
                size=8,
                is_binary=False,
                language="Python",
                content="print(1)\n",
            ),
            ProjectFile(
                project_id=project.id,
                path="src/lib/helper.py",
                size=4,
                is_binary=False,
                language="Python",
                content="def h(): pass",
            ),
            ProjectFile(
                project_id=project.id,
                path="assets/logo.png",
                size=512,
                is_binary=True,
                language=None,
                content=None,
            ),
            ProjectFile(
                project_id=project.id,
                path="big/generated.py",
                size=900,
                is_binary=False,
                language="Python",
                content=None,
            ),
        ]
    )
    db.session.commit()
    return workspace, project


def _export(client, project_id):
    return client.get(f"/workspaces/api/projects/{project_id}/export")


def _zip_entries(response) -> dict:
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    return {name: archive.read(name) for name in archive.namelist()}


class TestExportContent:
    def test_streams_zip_with_stored_text_files(self, client, app, make_user, login):
        _, project = _setup(make_user, login)
        response = _export(client, project.id)
        assert response.status_code == 200
        assert response.mimetype == "application/zip"
        assert "attachment" in response.headers["Content-Disposition"]
        assert response.headers["Content-Disposition"].endswith('-export.zip"')

        entries = _zip_entries(response)
        assert entries["app.py"] == b"print(1)\n"
        assert entries["src/lib/helper.py"] == b"def h(): pass"

    def test_binary_and_oversized_files_become_placeholders(self, client, app, make_user, login):
        _, project = _setup(make_user, login)
        response = _export(client, project.id)
        entries = _zip_entries(response)

        binary_stub = entries["assets/logo.png.PLACEHOLDER.txt"].decode()
        assert "NOT included" in binary_stub
        assert "binary" in binary_stub
        assert "Original size: 512 bytes" in binary_stub

        oversized_stub = entries["big/generated.py.PLACEHOLDER.txt"].decode()
        assert "NOT included" in oversized_stub
        assert "larger than the stored-content cap" in oversized_stub

        # The originals must never appear as (empty) real entries.
        assert "assets/logo.png" not in entries
        assert "big/generated.py" not in entries

    def test_manifest_summarizes_export(self, client, app, make_user, login):
        _, project = _setup(make_user, login)
        response = _export(client, project.id)
        manifest = json.loads(_zip_entries(response)["EXPORT-MANIFEST.json"].decode())
        assert manifest["kind"] == "ai-code-assistant-project-export"
        assert manifest["project"]["id"] == project.id
        assert manifest["project"]["name"] == "Demo Project"
        assert manifest["included_files"] == 2
        assert manifest["placeholder_files"] == 2

    def test_placeholder_text_is_capped(self, client, app, make_user, login):
        _, project = _setup(make_user, login)
        app.config["PROJECT_EXPORT_PLACEHOLDER_MAX_CHARS"] = 50
        response = _export(client, project.id)
        stub = _zip_entries(response)["assets/logo.png.PLACEHOLDER.txt"].decode()
        assert len(stub) <= 50

    def test_directory_structure_is_preserved(self, client, app, make_user, login):
        _, project = _setup(make_user, login)
        response = _export(client, project.id)
        names = set(zipfile.ZipFile(io.BytesIO(response.data)).namelist())
        assert "src/lib/helper.py" in names


class TestExportGuards:
    def test_requires_login(self, client, make_user):
        _setup(make_user, lambda **_: None)
        response = client.get("/workspaces/api/projects/1/export")
        assert response.status_code == 302

    def test_other_user_gets_404(self, client, app, make_user, login):
        _, project = _setup(make_user, login, username="alice", email="alice@example.com")
        make_user(username="bob", email="bob@example.com")
        login(email="bob@example.com")
        assert _export(client, project.id).status_code == 404

    def test_indexing_project_is_conflict(self, client, app, make_user, login):
        user = make_user(username="indexing", email="indexing@example.com")
        login(email="indexing@example.com")
        workspace = Workspace(user_id=user.id, name="W")
        db.session.add(workspace)
        db.session.commit()
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Busy",
            status=STATUS_INDEXING,
        )
        db.session.add(project)
        db.session.commit()
        response = _export(client, project.id)
        assert response.status_code == 409
        assert "indexing" in response.get_json()["error"]

    def test_failed_project_still_exports(self, client, app, make_user, login):
        """Only *indexing* is blocked; failed exports what was stored."""
        _, project = _setup(make_user, login)
        project.status = "failed"
        db.session.commit()
        assert _export(client, project.id).status_code == 200


class TestExportSideEffects:
    def test_records_activity_event(self, client, app, make_user, login):
        _, project = _setup(make_user, login)
        _export(client, project.id)
        from app.models.activity_event import ActivityEvent

        event = ActivityEvent.query.filter_by(event_type=EVENT_PROJECT_EXPORTED).first()
        assert event is not None
        assert event.target_id == project.id
        assert event.event_metadata["file_count"] == 3

    def test_rate_limited_after_max_exports(self, client, app, make_user, login):
        _, project = _setup(make_user, login)
        app.config["RATE_LIMIT_EXPORT_MAX"] = 2
        app.config["RATE_LIMIT_EXPORT_WINDOW"] = 60
        assert _export(client, project.id).status_code == 200
        assert _export(client, project.id).status_code == 200
        response = _export(client, project.id)
        assert response.status_code == 429
        assert "Retry-After" in response.headers
