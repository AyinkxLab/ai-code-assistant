"""Tests for background project indexing (#89).

Imports run in an in-process worker so the HTTP request returns immediately
(project in ``indexing`` status) and the client polls ``status``/``progress``;
failures mark the project failed with a stored error message.
"""

import io
import zipfile

import pytest

from app.extensions import db
from app.models import Project, User, Workspace
from app.models.project import (
    SOURCE_ARCHIVE,
    STATUS_FAILED,
    STATUS_INDEXING,
    STATUS_READY,
)
from app.services import import_jobs


class FakeExecutor:
    """Records submitted jobs instead of running them, for deterministic tests."""

    def __init__(self):
        self.submissions = []

    def submit(self, fn, *args, **kwargs):
        self.submissions.append((fn, args, kwargs))
        return None


@pytest.fixture()
def fake_async(app):
    """Enable async imports and capture jobs with a fake executor."""
    original = import_jobs.get_executor()
    app.config["IMPORT_JOBS_ASYNC"] = True
    fake = FakeExecutor()
    import_jobs.set_executor(fake)
    try:
        yield fake
    finally:
        import_jobs.set_executor(original)


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _seed_project(db, *, status=STATUS_INDEXING):
    user = User(username="jobuser", email="jobuser@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="Job workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Job project",
        source=SOURCE_ARCHIVE,
        status=status,
        progress=0,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _workspace_and_login(make_user, login):
    user = make_user(username="importer", email="importer@example.com")
    login(email="importer@example.com")
    workspace = Workspace(user_id=user.id, name="Import workspace")
    db.session.add(workspace)
    db.session.commit()
    return user, workspace


class TestRunImportJob:
    def test_success_marks_ready_at_100(self, app, db):
        project = _seed_project(db)
        project_id = project.id

        seen = []

        def task(proj, user, set_progress):
            set_progress(50)
            seen.append(db.session.get(Project, proj.id).progress)

        import_jobs.run_import_job(app, project_id, task)

        db.session.expire_all()
        refreshed = db.session.get(Project, project_id)
        assert refreshed.status == STATUS_READY
        assert refreshed.progress == 100
        assert refreshed.error_message is None
        assert refreshed.indexed_at is not None
        assert seen == [50]

    def test_failure_marks_failed_with_error_message(self, app, db):
        project = _seed_project(db)
        project_id = project.id

        def task(proj, user, set_progress):
            raise RuntimeError("boom during import")

        import_jobs.run_import_job(app, project_id, task)

        db.session.expire_all()
        refreshed = db.session.get(Project, project_id)
        assert refreshed.status == STATUS_FAILED
        assert "boom during import" in (refreshed.error_message or "")
        assert refreshed.progress == 0

    def test_missing_project_is_a_noop(self, app):
        import_jobs.run_import_job(app, 999999, lambda *a, **k: None)


class TestAsyncImportRoute:
    def test_archive_import_returns_indexing_then_completes(
        self, client, app, make_user, login, fake_async
    ):
        _user, workspace = _workspace_and_login(make_user, login)
        payload = _zip_bytes([("app.py", b"print(1)"), ("README.md", b"# hi")])

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            data={"file": (io.BytesIO(payload), "proj.zip")},
            content_type="multipart/form-data",
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["status"] == STATUS_INDEXING
        assert body["progress"] == 0
        assert len(fake_async.submissions) == 1

        # Run the queued job, then confirm the terminal state.
        fn, args, kwargs = fake_async.submissions[0]
        fn(*args, **kwargs)

        db.session.expire_all()
        project = db.session.get(Project, body["id"])
        assert project.status == STATUS_READY
        assert project.progress == 100
        assert project.file_count == 2

    def test_archive_failure_marks_failed(self, client, app, make_user, login, fake_async):
        _user, workspace = _workspace_and_login(make_user, login)

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            data={"file": (io.BytesIO(b"definitely not a zip"), "bad.zip")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 201
        project_id = response.get_json()["id"]

        fn, args, kwargs = fake_async.submissions[0]
        fn(*args, **kwargs)

        db.session.expire_all()
        project = db.session.get(Project, project_id)
        assert project.status == STATUS_FAILED
        assert project.error_message
        assert project.progress == 0

    def test_github_import_async_completes(
        self, client, app, make_user, login, fake_async, monkeypatch
    ):
        _user, workspace = _workspace_and_login(make_user, login)
        monkeypatch.setattr("app.workspaces.routes.get_github_client", lambda *a, **k: object())
        monkeypatch.setattr(
            "app.workspaces.routes.import_github_repo",
            lambda project, full_name, client: None,
        )

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={"source": "github", "repo": "owner/repo"},
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["status"] == STATUS_INDEXING
        assert len(fake_async.submissions) == 1

        fn, args, kwargs = fake_async.submissions[0]
        fn(*args, **kwargs)

        db.session.expire_all()
        project = db.session.get(Project, body["id"])
        assert project.status == STATUS_READY
        assert project.progress == 100


class TestSyncModeUnchanged:
    def test_scaffold_import_still_synchronous(self, client, app, make_user, login):
        _user, workspace = _workspace_and_login(make_user, login)
        # TestingConfig sets IMPORT_JOBS_ASYNC=False -> imports complete inline.
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            json={"source": "scaffold", "name": "my-vault"},
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["status"] == STATUS_READY
        assert body["progress"] == 100

    def test_bad_archive_returns_400_in_sync_mode(self, client, app, make_user, login):
        _user, workspace = _workspace_and_login(make_user, login)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            data={"file": (io.BytesIO(b"not a zip"), "bad.zip")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
