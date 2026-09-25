"""Re-import / refresh a project from its source (issue #88)."""

from app.extensions import db
from app.models import Project, ProjectFile, Workspace
from app.models.project import (
    SOURCE_ARCHIVE,
    SOURCE_GITHUB,
    SOURCE_SCAFFOLD,
    STATUS_INDEXING,
    STATUS_READY,
)
from app.services.github import GitHubError
from app.services.importing import store_project_files


def _workspace(user):
    workspace = Workspace(user_id=user.id, name="Refresh workspace")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _project(user, workspace, **overrides):
    fields = {
        "workspace_id": workspace.id,
        "user_id": user.id,
        "name": "my-project",
        "source": SOURCE_ARCHIVE,
        "status": STATUS_READY,
    }
    fields.update(overrides)
    project = Project(**fields)
    db.session.add(project)
    db.session.commit()
    return project


def _file(project, path="old.py", content="old"):
    db.session.add(
        ProjectFile(
            project_id=project.id,
            path=path,
            size=len(content),
            is_binary=False,
            language="python",
            content=content,
        )
    )
    db.session.commit()


def _refresh(client, project):
    return client.post(
        f"/workspaces/api/projects/{project.id}/refresh",
        headers={"X-CSRFToken": "ignored"},
    )


class TestRefreshSource:
    def test_github_refresh_replaces_file_set(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        workspace = _workspace(user)
        project = _project(user, workspace, source=SOURCE_GITHUB, source_url="owner/repo")
        _file(project, "old.py")

        monkeypatch.setattr("app.workspaces.routes.get_github_client", lambda *a, **k: object())

        def fake_import(project, full_name, client, **kwargs):
            assert full_name == "owner/repo"
            store_project_files(
                project,
                [
                    {
                        "path": "new.py",
                        "size": 3,
                        "is_binary": False,
                        "language": "python",
                        "content": "new",
                    }
                ],
            )

        monkeypatch.setattr("app.workspaces.routes.import_github_repo", fake_import)

        response = _refresh(client, project)

        assert response.status_code == 200
        paths = {f.path for f in ProjectFile.query.filter_by(project_id=project.id).all()}
        assert paths == {"new.py"}

    def test_scaffold_refresh_regenerates_files(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user)
        project = _project(user, workspace, source=SOURCE_SCAFFOLD, name="my-vault")
        _file(project, "stale.txt")

        response = _refresh(client, project)

        assert response.status_code == 200
        files = ProjectFile.query.filter_by(project_id=project.id).all()
        assert files
        assert all(f.path != "stale.txt" for f in files)

    def test_archive_refresh_is_rejected(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user)
        project = _project(user, workspace, source=SOURCE_ARCHIVE)

        response = _refresh(client, project)

        assert response.status_code == 409
        assert "archive" in response.get_json()["error"].lower()

    def test_indexing_project_is_rejected(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user)
        project = _project(
            user, workspace, source=SOURCE_GITHUB, source_url="o/r", status=STATUS_INDEXING
        )

        response = _refresh(client, project)

        assert response.status_code == 409

    def test_github_failure_is_reported(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        workspace = _workspace(user)
        project = _project(user, workspace, source=SOURCE_GITHUB, source_url="owner/repo")
        monkeypatch.setattr("app.workspaces.routes.get_github_client", lambda *a, **k: object())

        def boom(project, full_name, client, **kwargs):
            raise GitHubError("upstream down")

        monkeypatch.setattr("app.workspaces.routes.import_github_repo", boom)

        response = _refresh(client, project)

        assert response.status_code == 502

    def test_non_owner_gets_404(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="other", email="other@example.com")
        workspace = _workspace(owner)
        project = _project(owner, workspace, source=SOURCE_GITHUB, source_url="o/r")
        login(email="other@example.com")

        response = _refresh(client, project)

        assert response.status_code == 404


class TestRefreshButton:
    def test_button_shown_for_ready_github_project(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user)
        project = _project(user, workspace, source=SOURCE_GITHUB, source_url="o/r")

        html = client.get(f"/workspaces/{workspace.id}/projects/{project.id}").get_data(
            as_text=True
        )

        assert 'id="refresh-project"' in html

    def test_button_hidden_for_archive_project(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _workspace(user)
        project = _project(user, workspace, source=SOURCE_ARCHIVE)

        html = client.get(f"/workspaces/{workspace.id}/projects/{project.id}").get_data(
            as_text=True
        )

        assert 'id="refresh-project"' not in html
