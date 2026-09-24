"""Tests for project-wide search."""

import pytest

from app.extensions import db
from app.models import Project, ProjectFile, User, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services.search import SearchQueryError, search_project


def _ready_project(files):
    user = User(username="searchuser", email="searchuser@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="Search workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Search project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()

    for path, content, is_binary in files:
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path=path,
                size=len(content or b""),
                is_binary=is_binary,
                content=None if is_binary else content,
            )
        )
    db.session.commit()
    return project


class TestPathSearch:
    def test_matches_file_name(self, app):
        project = _ready_project([("login_form.py", "x = 1", False), ("other.py", "y", False)])
        result = search_project(project.id, "login")
        assert result["total"] == 1
        assert result["results"][0]["path"] == "login_form.py"
        assert result["results"][0]["matched"] == "path"

    def test_empty_query_returns_nothing(self, app):
        project = _ready_project([("app.py", "x", False)])
        result = search_project(project.id, "   ")
        assert result["total"] == 0

    def test_path_match_sorted_before_content(self, app):
        project = _ready_project(
            [
                ("tools/search_helper.py", "a", False),
                ("helper.py", "def search_here(): pass", False),
            ]
        )
        result = search_project(project.id, "search")
        assert result["results"][0]["matched"] == "path"


class TestContentSearch:
    def test_matches_contents_with_snippet(self, app):
        content = "def main():\n    return 'unique_token_xyz'\n"
        project = _ready_project([("app.py", content, False)])
        result = search_project(project.id, "unique_token_xyz")
        assert result["total"] == 1
        hit = result["results"][0]
        assert hit["matched"] == "content"
        assert hit["snippet"] is not None
        assert "unique_token_xyz" in hit["snippet"]

    def test_binary_files_excluded_from_content_search(self, app):
        project = _ready_project(
            [("blob.dat", b"\x00secret_token\x00", True), ("text.py", "secret_token", False)]
        )
        result = search_project(project.id, "secret_token")
        paths = [r["path"] for r in result["results"]]
        assert "text.py" in paths
        assert "blob.dat" not in paths

    def test_case_sensitive_query(self, app):
        project = _ready_project([("a.py", "FooBar", False)])
        assert search_project(project.id, "foobar")["total"] == 1
        assert search_project(project.id, "FooBar", case_sensitive=True)["total"] == 1

    def test_limit_caps_results(self, app):
        project = _ready_project([(f"file_{i}.py", "needle", False) for i in range(20)])
        result = search_project(project.id, "needle", limit=5)
        assert result["total"] == 5

    def test_escape_like_wildcards(self, app):
        project = _ready_project([("app.py", "has 100% certainty", False)])
        result = search_project(project.id, "100%")
        assert result["total"] == 1


def _typed_project(files):
    """Create a ready project where each file carries an explicit language."""
    user = User(username="typeduser", email="typeduser@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="Typed workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Typed project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    for path, content, language, is_binary, has_content in files:
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path=path,
                size=len(content or ""),
                language=language,
                is_binary=is_binary,
                content=(content if has_content else None),
            )
        )
    db.session.commit()
    return project


class TestScopeFilter:
    def test_path_only_scope_ignores_content(self, app):
        project = _ready_project(
            [("needle.py", "nothing here", False), ("other.py", "needle", False)]
        )
        result = search_project(project.id, "needle", scope="path")
        assert [r["path"] for r in result["results"]] == ["needle.py"]
        assert result["results"][0]["matched"] == "path"

    def test_content_only_scope_ignores_paths(self, app):
        project = _ready_project(
            [("needle.py", "nothing here", False), ("other.py", "needle", False)]
        )
        result = search_project(project.id, "needle", scope="content")
        assert [r["path"] for r in result["results"]] == ["other.py"]
        assert result["results"][0]["matched"] == "content"

    def test_invalid_scope_raises(self, app):
        project = _ready_project([("app.py", "x", False)])
        with pytest.raises(SearchQueryError):
            search_project(project.id, "x", scope="bogus")


class TestLanguageFilter:
    def test_language_filter_restricts_results(self, app):
        project = _typed_project(
            [
                ("a.py", "shared_token", "python", False, True),
                ("b.js", "shared_token", "javascript", False, True),
            ]
        )
        result = search_project(project.id, "shared_token", language="python")
        assert [r["path"] for r in result["results"]] == ["a.py"]
        # Case-insensitive language match.
        assert search_project(project.id, "shared_token", language="PYTHON")["total"] == 1

    def test_language_filter_composes_with_scope(self, app):
        project = _typed_project(
            [
                ("a.py", "content_token", "python", False, True),
                ("a.js", "content_token", "javascript", False, True),
            ]
        )
        result = search_project(project.id, "a.", language="javascript", scope="path")
        assert [r["path"] for r in result["results"]] == ["a.js"]


class TestRegexMode:
    def test_regex_matches_path_and_content(self, app):
        project = _ready_project(
            [("handler_01.py", "def run(): pass", False), ("notes.txt", "handler 02", False)]
        )
        result = search_project(project.id, r"handler[_ ]\d+", regex=True)
        paths = sorted(r["path"] for r in result["results"])
        assert paths == ["handler_01.py", "notes.txt"]

    def test_invalid_regex_raises(self, app):
        project = _ready_project([("app.py", "x", False)])
        with pytest.raises(SearchQueryError):
            search_project(project.id, "(unclosed", regex=True)

    def test_regex_respects_limit(self, app):
        project = _ready_project([(f"item_{i}.py", "body", False) for i in range(20)])
        result = search_project(project.id, r"item_\d+", regex=True, limit=5)
        assert result["total"] == 5

    def test_regex_excludes_binary_and_contentless_from_content(self, app):
        project = _typed_project(
            [
                ("blob.bin", "secret_token", None, True, True),
                ("nocontent.py", "", "python", False, False),
                ("real.py", "secret_token", "python", False, True),
            ]
        )
        result = search_project(project.id, "secret_token", regex=True, scope="content")
        assert [r["path"] for r in result["results"]] == ["real.py"]
