"""Tests for Phase 5 models: workspaces, projects, project files, and chat."""

from app.extensions import db
from app.models import Project, ProjectChatSession, ProjectFile, ProjectMessage, User, Workspace
from app.models.project import (
    SOURCE_ARCHIVE,
    SOURCE_GITHUB,
    STATUS_INDEXING,
    STATUS_READY,
)


def _make_user(username="moduser", email="moduser@example.com"):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _make_workspace(user, name="Workspace"):
    workspace = Workspace(user_id=user.id, name=name, description="A test workspace")
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestWorkspaceModel:
    def test_create_and_serialize(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        payload = workspace.to_dict()
        assert payload["name"] == "Workspace"
        assert payload["description"] == "A test workspace"
        assert payload["project_count"] == 0
        assert payload["id"] == workspace.id

    def test_project_count_reflects_imports(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        db.session.add(
            Project(workspace_id=workspace.id, user_id=user.id, name="p", source=SOURCE_ARCHIVE)
        )
        db.session.commit()
        assert workspace.to_dict()["project_count"] == 1

    def test_cascade_delete_removes_projects_files_messages(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = Project(
            workspace_id=workspace.id, user_id=user.id, name="p", source=SOURCE_GITHUB
        )
        db.session.add(project)
        db.session.commit()
        db.session.add(
            ProjectFile(
                project_id=project.id, path="app.py", size=5, is_binary=False, content="print()"
            )
        )
        chat_session = ProjectChatSession(project_id=project.id, title="Chat")
        db.session.add(chat_session)
        db.session.commit()
        db.session.add(
            ProjectMessage(
                project_id=project.id,
                session_id=chat_session.id,
                role="user",
                content="hi",
            )
        )
        db.session.commit()

        db.session.delete(workspace)
        db.session.commit()

        assert Project.query.count() == 0
        assert ProjectFile.query.count() == 0
        assert ProjectMessage.query.count() == 0


class TestProjectModel:
    def test_defaults(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = Project(workspace_id=workspace.id, user_id=user.id, name="demo")
        db.session.add(project)
        db.session.commit()
        assert project.status == STATUS_INDEXING
        assert project.file_count == 0
        assert project.total_size_bytes == 0
        assert project.source == SOURCE_ARCHIVE
        assert project.error_message is None

    def test_serialize_includes_stats(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = Project(
            workspace_id=workspace.id,
            user_id=user.id,
            name="demo",
            source=SOURCE_GITHUB,
            source_url="owner/demo",
            status=STATUS_READY,
            file_count=3,
            total_size_bytes=100,
        )
        db.session.add(project)
        db.session.commit()
        payload = project.to_dict()
        assert payload["source"] == SOURCE_GITHUB
        assert payload["source_url"] == "owner/demo"
        assert payload["status"] == STATUS_READY
        assert payload["file_count"] == 3
        assert payload["total_size_bytes"] == 100


class TestProjectFileModel:
    def test_unique_path_per_project(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = Project(workspace_id=workspace.id, user_id=user.id, name="p")
        db.session.add(project)
        db.session.commit()
        db.session.add(
            ProjectFile(project_id=project.id, path="a.py", size=1, is_binary=False, content="x")
        )
        db.session.commit()
        duplicate = ProjectFile(
            project_id=project.id, path="a.py", size=1, is_binary=False, content="y"
        )
        db.session.add(duplicate)
        try:
            db.session.commit()
            raise AssertionError("Expected a unique constraint violation.")
        except Exception:
            db.session.rollback()

    def test_serialize_never_includes_content(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = Project(workspace_id=workspace.id, user_id=user.id, name="p")
        db.session.add(project)
        db.session.commit()
        file = ProjectFile(
            project_id=project.id, path="app.py", size=6, is_binary=False, content="secret"
        )
        db.session.add(file)
        db.session.commit()
        payload = file.to_dict()
        assert "content" not in payload
        assert payload["searchable"] is True


class TestProjectMessageModel:
    def test_serialize(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = Project(workspace_id=workspace.id, user_id=user.id, name="p")
        db.session.add(project)
        db.session.commit()
        chat_session = ProjectChatSession(project_id=project.id, title="Chat")
        db.session.add(chat_session)
        db.session.commit()
        message = ProjectMessage(
            project_id=project.id,
            session_id=chat_session.id,
            role="assistant",
            content="reply",
        )
        db.session.add(message)
        db.session.commit()
        payload = message.to_dict()
        assert payload["role"] == "assistant"
        assert payload["content"] == "reply"
