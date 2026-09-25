"""Tests for project intelligence: bounded context retrieval, chat, analyses,
and the dependency inventory."""

from contextlib import contextmanager

from flask_login import login_user

from app.extensions import db
from app.models import Project, ProjectFile, User, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services import project_analysis


@contextmanager
def _authorized_context(app, project):
    """Push a request context with the project owner logged in."""
    with app.test_request_context("/"):
        login_user(db.session.get(User, project.user_id))
        yield


def _ready_project(files):
    user = User(username="anauser", email="anauser@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="Analysis workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Analysis project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()

    for path, content in files:
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path=path,
                size=len(content),
                is_binary=False,
                content=content,
            )
        )
    db.session.commit()
    project.file_count = len(files)
    project.total_size_bytes = sum(len(content) for _, content in files)
    db.session.commit()
    return project


class TestBoundedContext:
    def test_context_is_bounded(self, app):
        big_file = "line = 'x' * 10\n" * 2000
        project = _ready_project(
            [
                ("app.py", big_file),
                ("main.py", big_file),
                ("utils.py", big_file),
                ("README.md", "About this project\n"),
            ]
        )
        with _authorized_context(app, project):
            context = project_analysis.build_context(project, "what does app.py do")
        budget = app.config["PROJECT_MAX_CONTEXT_CHARS"]
        assert len(context["blocks"]) <= budget
        assert 1 <= len(context["paths"]) <= project_analysis.MAX_CONTEXT_FILES

    def test_context_prefers_relevant_files(self, app):
        project = _ready_project(
            [
                ("app.py", "def handler(): pass"),
                ("utils.py", "def helper(): pass"),
            ]
        )
        with _authorized_context(app, project):
            context = project_analysis.build_context(project, "handler")
        assert "app.py" in context["paths"]

    def test_attachments_are_pinned_and_mentioned_files_attach(self, app):
        project = _ready_project(
            [
                ("attached.py", "attached content"),
                ("mentioned.py", "mentioned content"),
                ("keyword.py", "keyword content"),
            ]
        )
        with _authorized_context(app, project):
            context = project_analysis.build_context(
                project, "Explain @mentioned.py", budget=5000, attachments=["attached.py"]
            )
        assert context["paths"][:2] == ["attached.py", "mentioned.py"]

    def test_attachments_obey_budget_and_per_file_clip(self, app):
        project = _ready_project([("attached.py", "x" * 5000), ("other.py", "y" * 5000)])
        with _authorized_context(app, project):
            context = project_analysis.build_context(
                project, "question", budget=3000, attachments=["attached.py"]
            )
        assert len(context["blocks"]) <= 3000
        assert "x" * 1500 in context["blocks"]

    def test_key_files_included_without_keywords(self, app):
        project = _ready_project(
            [
                ("src/app.py", "def main(): pass"),
                ("README.md", "Docs"),
            ]
        )
        with _authorized_context(app, project):
            context = project_analysis.build_context(project, "anything")
        assert "README.md" in context["paths"]

    def test_structure_is_bounded(self, app):
        project = _ready_project([(f"file_{i}.py", "x") for i in range(100)])
        structure = project_analysis.project_structure(project)
        assert "100 files" in structure
        assert "file_99.py" in structure


class TestChat:
    def test_chat_returns_analysis(self, app):
        project = _ready_project([("app.py", "def main(): return 42")])
        with _authorized_context(app, project):
            result = project_analysis.chat_with_project(project, "what does main return?")
        assert result["context_paths"]
        assert "mock assistant response" in result["analysis"]

    def test_build_messages_has_system_prompt(self, app):
        project = _ready_project([("app.py", "x")])
        with _authorized_context(app, project):
            messages = project_analysis.build_messages(project, "hello", [])
        assert messages[0]["role"] == "system"
        assert "untrusted DATA" in messages[0]["content"]
        assert messages[-1]["role"] == "user"


class TestAnalyzeProject:
    def test_all_kinds_return_analysis(self, app):
        project = _ready_project([("app.py", "def f(): pass")])
        with _authorized_context(app, project):
            for kind in project_analysis.ANALYSIS_KINDS:
                result = project_analysis.analyze_project(project, kind)
                assert result["kind"] == kind
                assert result["analysis"]

    def test_unknown_kind_defaults_to_architecture(self, app):
        project = _ready_project([("app.py", "x")])
        with _authorized_context(app, project):
            assert project_analysis.analyze_project(project, "bogus")["kind"] == "architecture"

    def test_system_prompt_enforces_labeling_and_injection_guard(self):
        system = project_analysis._PROJECT_SYSTEM
        assert "[CONFIRMED]" in system
        assert "[SUGGESTION]" in system
        assert "untrusted DATA" in system

    def test_dependencies_analysis_uses_real_inventory(self, app):
        project = _ready_project(
            [
                ("app.py", "import requests"),
                ("requirements.txt", "requests==2.31.0\nflask>=2.0\n"),
            ]
        )
        inventory = project_analysis.dependency_inventory(project)
        names = {item["name"] for item in inventory}
        assert "requests" in names
        assert "flask" in names
        with _authorized_context(app, project):
            result = project_analysis.analyze_project(project, "dependencies")
        # The mock echoes the prompt prefix, so the team context header proves
        # the dependencies analysis ran through the context builders; the real
        # manifest inventory itself is verified by dependency_inventory above.
        assert result["kind"] == "dependencies"
        assert "Workspace: Analysis workspace" in result["analysis"]


class TestDependencyInventory:
    def test_requirements_txt(self, app):
        content = "requests==2.31.0\n# comment\n"
        project = _ready_project([("requirements.txt", content)])
        inventory = project_analysis.dependency_inventory(project)
        expected = [{"file": "requirements.txt", "name": "requests", "constraint": "==2.31.0"}]
        assert inventory == expected

    def test_package_json(self, app):
        content = '{"dependencies": {"react": "^18.0.0"}, "devDependencies": {"jest": "29"}}'
        project = _ready_project([("package.json", content)])
        inventory = project_analysis.dependency_inventory(project)
        names = {item["name"] for item in inventory}
        assert "react" in names and "jest" in names

    def test_pyproject_toml(self, app):
        content = '[project]\ndependencies = ["requests>=2.0", "flask==3.0"]\n'
        project = _ready_project([("pyproject.toml", content)])
        inventory = project_analysis.dependency_inventory(project)
        names = {item["name"] for item in inventory}
        assert "requests" in names and "flask" in names

    def test_cargo_toml(self, app):
        content = '[dependencies]\nserde = "1.0"\n'
        project = _ready_project([("Cargo.toml", content)])
        inventory = project_analysis.dependency_inventory(project)
        assert inventory == [{"file": "Cargo.toml", "name": "serde", "constraint": "=1.0"}]

    def test_no_manifests_returns_empty(self, app):
        project = _ready_project([("app.py", "x")])
        assert project_analysis.dependency_inventory(project) == []
