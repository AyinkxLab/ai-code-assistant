"""Tests for the chat project file tree API (#43).

The chat sidebar renders a collapsible tree grouped by workspace → project →
file; opening a file uses the workspace file endpoint (which returns the
``language`` + ``content`` that back the syntax-highlighted view).
"""

from app.extensions import db
from app.models import Project, ProjectFile, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_INDEXING, STATUS_READY


def _seed(make_user, login, *, ready=True, files=(("app.py", "print(1)"), ("README.md", "# hi"))):
    user = make_user(username="chatuser", email="chatuser@example.com")
    login(email="chatuser@example.com")
    workspace = Workspace(user_id=user.id, name="Chat workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Chat project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY if ready else STATUS_INDEXING,
    )
    db.session.add(project)
    db.session.commit()
    rows = []
    for path, content in files:
        row = ProjectFile(
            project_id=project.id,
            path=path,
            size=len(content),
            is_binary=False,
            language="python" if path.endswith(".py") else None,
            content=content,
        )
        db.session.add(row)
        rows.append(row)
    db.session.commit()
    return user, workspace, project, rows


class TestChatProjectFilesApi:
    def test_requires_login(self, client):
        assert client.get("/chat/api/project-files").status_code == 302

    def test_empty_state(self, client, make_user, login):
        make_user()
        login()
        assert client.get("/chat/api/project-files").get_json() == []

    def test_grouped_tree(self, client, app, make_user, login):
        _user, _workspace, project, _rows = _seed(make_user, login)

        data = client.get("/chat/api/project-files").get_json()
        assert len(data) == 1
        assert data[0]["name"] == "Chat workspace"

        projects = data[0]["projects"]
        assert len(projects) == 1
        assert projects[0]["status"] == "ready"
        assert projects[0]["truncated"] is False

        files = projects[0]["files"]
        assert [f["path"] for f in files] == ["README.md", "app.py"]
        by_path = {f["path"]: f for f in files}
        assert by_path["app.py"]["size"] == len("print(1)")
        assert by_path["app.py"]["language"] == "python"
        assert by_path["app.py"]["created_at"]
        assert by_path["app.py"]["project_id"] == project.id

    def test_indexing_project_listed_without_files(self, client, make_user, login):
        _seed(make_user, login, ready=False, files=())
        data = client.get("/chat/api/project-files").get_json()
        assert data[0]["projects"][0]["status"] == "indexing"
        assert data[0]["projects"][0]["files"] == []

    def test_owner_scoped(self, client, make_user, login):
        other = make_user(username="other", email="other@example.com")
        workspace = Workspace(user_id=other.id, name="Other workspace")
        db.session.add(workspace)
        db.session.commit()

        make_user(username="me", email="me@example.com")
        login(email="me@example.com")
        assert client.get("/chat/api/project-files").get_json() == []

    def test_truncation_flag(self, client, app, make_user, login, monkeypatch):
        monkeypatch.setattr("app.chat.routes.MAX_TREE_FILES", 1)
        _seed(make_user, login)
        data = client.get("/chat/api/project-files").get_json()
        project_payload = data[0]["projects"][0]
        assert project_payload["truncated"] is True
        assert len(project_payload["files"]) == 1


class TestFileView:
    def test_file_endpoint_backs_highlighted_view(self, client, app, make_user, login):
        _user, _workspace, project, _rows = _seed(make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/file?path=app.py")
        assert response.status_code == 200
        body = response.get_json()
        assert body["language"] == "python"
        assert body["content"] == "print(1)"

    def test_highlighter_asset_is_served(self, client, make_user, login):
        make_user()
        login()
        response = client.get("/static/js/chat_files.js")
        assert response.status_code == 200
        assert b"function highlight" in response.data
