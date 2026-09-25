"""Tests for the repository-analysis tool (issue #75).

The tool lives at ``POST /tools/repo-analyze`` and reuses
``app/services/analysis.py``. GitHub access is mocked at the client layer so the
tests never hit the network.
"""

from app.services import analysis
from app.services.github import GitHubNotConnectedError


class _FakeGitHubClient:
    def __init__(self, *, tree=None, readme="# Demo\nA demo repository.", files=None):
        self._tree = tree if tree is not None else {"tree": []}
        self._readme = readme
        self._files = files or {}

    def get_repository(self, full_name):
        return {"full_name": full_name, "default_branch": "main"}

    def get_readme(self, full_name, ref=None):
        return self._readme

    def get_tree(self, full_name, ref, recursive=True):
        return self._tree

    def get_file_text(self, full_name, path, ref=None):
        return self._files.get(path, "")


_DEFAULT_TREE = {
    "tree": [
        {"path": "README.md", "type": "blob"},
        {"path": "requirements.txt", "type": "blob"},
        {"path": "app/main.py", "type": "blob"},
        {"path": "app", "type": "tree"},
    ]
}


def _login(make_user, login):
    make_user()
    login()


class TestRepoAnalyzeRoute:
    def test_requires_login(self, client):
        response = client.post("/tools/repo-analyze", json={"owner": "o", "repo": "r"})
        assert response.status_code == 302

    def test_rejects_invalid_repository_name(self, client, make_user, login):
        _login(make_user, login)
        response = client.post("/tools/repo-analyze", json={"full_name": "not a repo"})
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_reports_missing_github_connection(self, client, make_user, login, monkeypatch):
        _login(make_user, login)

        def _raise():
            raise GitHubNotConnectedError("Connect your GitHub account.")

        monkeypatch.setattr("app.tools.routes.get_github_client", _raise)
        response = client.post("/tools/repo-analyze", json={"owner": "o", "repo": "r"})
        assert response.status_code == 502
        assert response.get_json()["kind"] == "not_connected"

    def test_analyzes_structure_dependencies_and_entry_points(
        self, client, make_user, login, monkeypatch
    ):
        _login(make_user, login)
        fake = _FakeGitHubClient(tree=_DEFAULT_TREE, files={"requirements.txt": "flask==3.1.1\n"})
        monkeypatch.setattr("app.tools.routes.get_github_client", lambda: fake)

        response = client.post("/tools/repo-analyze", json={"owner": "owner", "repo": "repo"})

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["kind"] == "repository"
        assert payload["full_name"] == "owner/repo"
        assert payload["context"]["dependency_manifests"] == ["requirements.txt"]
        assert payload["context"]["entry_points"] == ["app/main.py"]
        assert payload["structure"]["file_count"] == 3
        assert payload["structure"]["truncated"] is False
        assert "mock assistant response" in payload["analysis"].lower()

    def test_accepts_full_name_payload(self, client, make_user, login, monkeypatch):
        _login(make_user, login)
        fake = _FakeGitHubClient(tree=_DEFAULT_TREE)
        monkeypatch.setattr("app.tools.routes.get_github_client", lambda: fake)
        response = client.post("/tools/repo-analyze", json={"full_name": "owner/repo"})
        assert response.status_code == 200
        assert response.get_json()["full_name"] == "owner/repo"


class _CapturingProvider:
    def __init__(self):
        self.messages = None

    def complete(self, messages):
        self.messages = messages
        return "[CONFIRMED] summary\n[SUGGESTION] maybe"


class TestAnalyzeRepositoryPrompt:
    def test_prompt_covers_structure_dependencies_and_entry_points(self, monkeypatch):
        provider = _CapturingProvider()
        monkeypatch.setattr(analysis, "get_provider", lambda: provider)

        result = analysis.analyze_repository(
            "owner/repo",
            readme="# Demo",
            structure=["app/main.py", "requirements.txt"],
            dependencies=[{"path": "requirements.txt", "content": "flask==3.1.1"}],
            entry_points=["app/main.py"],
        )

        assert result["kind"] == "repository"
        assert result["full_name"] == "owner/repo"
        assert result["context"]["entry_points"] == ["app/main.py"]
        assert result["analysis"] == "[CONFIRMED] summary\n[SUGGESTION] maybe"

        system, user = provider.messages
        assert system["role"] == "system"
        assert "[CONFIRMED]" in system["content"]
        assert "[SUGGESTION]" in system["content"]
        assert "Structure" in user["content"]
        assert "Dependencies" in user["content"]
        assert "Entry points" in user["content"]
        assert "requirements.txt" in user["content"]
        assert "app/main.py" in user["content"]
        assert "[CONFIRMED]" in user["content"]
        assert "[SUGGESTION]" in user["content"]
