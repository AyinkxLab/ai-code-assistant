"""Tests for project explorer routes: file tree, file viewer, search, chat,
analysis, stats, and ownership isolation."""

from app.extensions import db
from app.models import Project, ProjectFile, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_INDEXING, STATUS_READY


def _setup(app, make_user, login, username="projuser", email="projuser@example.com"):
    user = make_user(username=username, email=email)
    login(email=email)
    workspace = Workspace(user_id=user.id, name="Project workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Demo",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
        file_count=4,
        total_size_bytes=37,
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
                language="python",
                content="print(1)",
            ),
            ProjectFile(
                project_id=project.id,
                path="README.md",
                size=5,
                is_binary=False,
                language="markdown",
                content="# Hi",
            ),
            ProjectFile(
                project_id=project.id,
                path="src/lib/helper.py",
                size=4,
                is_binary=False,
                language="python",
                content="def h(): pass",
            ),
            ProjectFile(
                project_id=project.id,
                path="assets/logo.bin",
                size=20,
                is_binary=True,
                language=None,
                content=None,
            ),
        ]
    )
    db.session.commit()
    return workspace, project


class TestTree:
    def test_root_listing(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/tree")
        assert response.status_code == 200
        data = response.get_json()
        assert data["directories"] == ["assets", "src"]
        assert [f["path"] for f in data["files"]] == ["README.md", "app.py"]

    def test_subdirectory_listing(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/tree?path=src/lib")
        assert response.status_code == 200
        data = response.get_json()
        assert data["directories"] == []
        assert data["files"][0]["path"] == "src/lib/helper.py"

    def test_traversal_rejected(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/tree?path=../../etc")
        assert response.status_code == 400


class TestFile:
    def test_file_contents(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/file?path=app.py")
        assert response.status_code == 200
        data = response.get_json()
        assert data["content"] == "print(1)"
        assert data["searchable"] is True

    def test_binary_file_has_no_content(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/file?path=assets/logo.bin")
        data = response.get_json()
        assert data["is_binary"] is True
        assert data["content"] is None
        assert data["searchable"] is False

    def test_unknown_file_404(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/file?path=nope.txt")
        assert response.status_code == 404

    def test_traversal_rejected(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/file?path=../../secret")
        assert response.status_code == 400


class TestSearchRoute:
    def test_search_returns_results(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/search?q=helper")
        assert response.status_code == 200
        data = response.get_json()
        assert data["total"] == 1
        assert data["results"][0]["path"] == "src/lib/helper.py"

    def test_search_requires_query(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/search")
        assert response.status_code == 400

    def test_scope_path_and_content(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        path_only = client.get(f"/workspaces/api/projects/{project.id}/search?q=app&scope=path")
        assert [r["path"] for r in path_only.get_json()["results"]] == ["app.py"]

        content_only = client.get(
            f"/workspaces/api/projects/{project.id}/search?q=app&scope=content"
        )
        assert content_only.get_json()["results"] == []

    def test_language_filter(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(
            f"/workspaces/api/projects/{project.id}/search?q=helper&language=python"
        )
        assert response.status_code == 200
        assert [r["path"] for r in response.get_json()["results"]] == ["src/lib/helper.py"]

        none = client.get(
            f"/workspaces/api/projects/{project.id}/search?q=helper&language=markdown"
        )
        assert none.get_json()["results"] == []

    def test_regex_mode_matches(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(
            f"/workspaces/api/projects/{project.id}/search?q=^src/&regex=1&scope=path"
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["regex"] is True
        assert [r["path"] for r in data["results"]] == ["src/lib/helper.py"]

    def test_filters_compose(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(
            f"/workspaces/api/projects/{project.id}/search?q=def&regex=1&scope=content&language=python"
        )
        data = response.get_json()
        assert [r["path"] for r in data["results"]] == ["src/lib/helper.py"]
        assert data["language"] == "python"
        assert data["scope"] == "content"

    def test_invalid_regex_is_400(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/search?q=(unclosed&regex=1")
        assert response.status_code == 400
        assert "regular expression" in response.get_json()["error"].lower()

    def test_invalid_scope_is_400(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/search?q=x&scope=bogus")
        assert response.status_code == 400

    def test_unindexed_project_conflict(self, client, app, make_user, login):
        user = make_user(username="idxuser", email="idxuser@example.com")
        login(email="idxuser@example.com")
        workspace = Workspace(user_id=user.id, name="W")
        db.session.add(workspace)
        db.session.commit()
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Not ready",
            status=STATUS_INDEXING,
        )
        db.session.add(project)
        db.session.commit()
        response = client.get(f"/workspaces/api/projects/{project.id}/search?q=x")
        assert response.status_code == 409


class TestChatRoute:
    def test_chat_persists_messages(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat",
            json={"content": "What does app.py do?"},
        )
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["assistant_message"]["role"] == "assistant"
        assert payload["context_paths"]
        history = client.get(f"/workspaces/api/projects/{project.id}/messages").get_json()
        assert [m["role"] for m in history] == ["user", "assistant"]

    def test_chat_requires_content(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.post(f"/workspaces/api/projects/{project.id}/chat", json={})
        assert response.status_code == 400

    def test_chat_stream_returns_events(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat/stream",
            json={"content": "Explain helper.py"},
        )
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        body = response.get_data(as_text=True)
        assert "data: " in body
        assert '"type": "done"' in body

    def test_chat_requires_indexed_project(self, client, app, make_user, login):
        user = make_user(username="idxuser2", email="idxuser2@example.com")
        login(email="idxuser2@example.com")
        workspace = Workspace(user_id=user.id, name="W")
        db.session.add(workspace)
        db.session.commit()
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Not ready",
            status=STATUS_INDEXING,
        )
        db.session.add(project)
        db.session.commit()
        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat", json={"content": "hi"}
        )
        assert response.status_code == 409


class TestAnalysisRoute:
    def test_analyze_valid_kind(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.post(
            f"/workspaces/api/projects/{project.id}/analyze", json={"kind": "bugs"}
        )
        assert response.status_code == 200
        assert response.get_json()["kind"] == "bugs"

    def test_analyze_invalid_kind(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.post(
            f"/workspaces/api/projects/{project.id}/analyze", json={"kind": "nonsense"}
        )
        assert response.status_code == 400


class TestStatsRoute:
    def test_stats_returns_real_metrics(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/api/projects/{project.id}/stats")
        assert response.status_code == 200
        data = response.get_json()
        assert data["file_count"] == 4
        assert data["searchable_file_count"] == 3
        assert data["test_file_count"] == 0
        assert data["dependency_count"] == 0
        assert data["languages"]
        assert data["project"]["id"] == project.id


class TestProjectIsolation:
    def test_other_user_cannot_access_project(self, client, app, make_user, login):
        _, project = _setup(app, make_user, login, username="alice", email="alice@example.com")
        make_user(username="bob", email="bob@example.com")
        login(email="bob@example.com")

        assert client.get(f"/workspaces/api/projects/{project.id}/tree").status_code == 404
        file_url = f"/workspaces/api/projects/{project.id}/file?path=app.py"
        assert client.get(file_url).status_code == 404
        assert client.get(f"/workspaces/api/projects/{project.id}/search?q=x").status_code == 404
        assert client.get(f"/workspaces/api/projects/{project.id}/stats").status_code == 404
        assert (
            client.post(
                f"/workspaces/api/projects/{project.id}/chat", json={"content": "hi"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/workspaces/api/projects/{project.id}/analyze", json={"kind": "bugs"}
            ).status_code
            == 404
        )
        assert client.delete(f"/workspaces/api/projects/{project.id}").status_code == 404
        explorer = f"/workspaces/{project.workspace_id}/projects/{project.id}"
        assert client.get(explorer).status_code == 404

    def test_project_explorer_page_renders(self, client, app, make_user, login):
        workspace, project = _setup(app, make_user, login)
        response = client.get(f"/workspaces/{workspace.id}/projects/{project.id}")
        assert response.status_code == 200
        assert b"AI Chat" in response.data
