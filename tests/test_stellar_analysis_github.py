"""Tests for Stellar/Soroban-aware PR and issue analysis (#203, #204).

Covers the detection-driven integration in ``app/services/analysis.py`` and the
GitHub routes: Stellar projects get a bounded Stellar context, non-Stellar and
plain-Rust projects keep the generic analysis, detection failures never crash
the flow, authorization is preserved, and context stays bounded. No live
Stellar networks are used.
"""

import base64
import json

from app.extensions import db
from app.models import GithubAccount, User
from app.services import analysis

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Fake LLM provider that records the user prompt it receives."""

    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, messages, *, stream=False):
        user = [m for m in messages if m.get("role") == "user"]
        self.prompts.append(user[-1]["content"] if user else "")
        return "mock analysis response"

    def stream(self, messages):
        return iter(["mock analysis response"])


def _install_provider(monkeypatch):
    provider = _RecordingProvider()
    monkeypatch.setattr(analysis, "get_provider", lambda: provider)
    return provider


def _pr(number=7, title="Update contracts"):
    return {
        "number": number,
        "title": title,
        "state": "open",
        "merged": False,
        "author": "alice",
        "base": "main",
        "head": "feature",
        "body": "Updates the token contract.",
    }


def _issue(number=3, title="Admin check missing"):
    return {
        "number": number,
        "title": title,
        "state": "open",
        "labels": ["bug"],
        "body": "The admin function has no authorization check.",
    }


def _soroban_pr_files():
    return [
        {
            "filename": "Cargo.toml",
            "status": "modified",
            "additions": 2,
            "deletions": 1,
            "patch": "+soroban-sdk = { version = '21.0.0' }",
        },
        {
            "filename": "contracts/token/src/lib.rs",
            "status": "modified",
            "additions": 10,
            "deletions": 2,
            "patch": "+#[contractimpl]\n+pub fn transfer(..) { .. }",
        },
    ]


def _plain_rust_pr_files():
    return [
        {
            "filename": "Cargo.toml",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "+serde = '1.0'",
        },
        {
            "filename": "src/main.rs",
            "status": "modified",
            "additions": 2,
            "deletions": 0,
            "patch": '+fn main() { println!("hi"); }',
        },
    ]


def _python_pr_files():
    return [
        {
            "filename": "app/x.py",
            "status": "modified",
            "additions": 3,
            "deletions": 1,
            "patch": "+def handler(): return 42",
        }
    ]


def _soroban_repo_files():
    return [
        {
            "path": "Cargo.toml",
            "content": (
                "[package]\nname='token'\n[dependencies]\nsoroban-sdk = { version = '21.0.0' }\n"
            ),
        },
        {"path": "src/lib.rs", "content": "#![no_std]\n#[contractimpl]\npub struct C {}\n"},
    ]


def _plain_rust_repo_files():
    return [
        {"path": "Cargo.toml", "content": "[dependencies]\nserde = '1.0'\n"},
        {"path": "src/main.rs", "content": "fn main() {}\n"},
    ]


def _python_repo_files():
    return [{"path": "app/__init__.py", "content": "APP = 'x'\n"}]


# ---------------------------------------------------------------------------
# analyze_pull_request (#203)
# ---------------------------------------------------------------------------


class TestPullRequestStellarAware:
    def test_stellar_pr_includes_context(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_pull_request(_pr(), _soroban_pr_files())
        assert result["stellar"]["detected"] is True
        assert result["stellar"]["confidence"] == "likely"
        prompt = provider.prompts[-1]
        assert "Stellar/Soroban project context" in prompt
        assert "soroban-sdk" in prompt
        assert "Changed files in scope:" in prompt
        assert "Do NOT claim formal verification" in prompt

    def test_non_stellar_pr_unchanged(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_pull_request(_pr(), _python_pr_files())
        assert result["stellar"]["detected"] is False
        prompt = provider.prompts[-1]
        assert "Stellar/Soroban project context" not in prompt
        assert "Provide a structured review with these sections:" in prompt

    def test_plain_rust_not_classified_as_stellar(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_pull_request(_pr(), _plain_rust_pr_files())
        assert result["stellar"]["detected"] is False
        prompt = provider.prompts[-1]
        assert "Stellar/Soroban project context" not in prompt

    def test_keyword_mention_not_enough(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        files = [
            {
                "filename": "README.md",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch": "+We love Stellar blockchain",
            }
        ]
        result = analysis.analyze_pull_request(_pr(), files)
        assert result["stellar"]["detected"] is False
        assert "Stellar/Soroban project context" not in provider.prompts[-1]

    def test_possible_confidence_is_respected(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        # A contracts/ directory without Soroban markers is only "possible".
        files = [
            {
                "filename": "contracts/hello/Cargo.toml",
                "status": "added",
                "additions": 3,
                "deletions": 0,
                "patch": "+[dependencies]\n+rand = '0.8'",
            }
        ]
        result = analysis.analyze_pull_request(_pr(), files)
        assert result["stellar"]["detected"] is True
        assert result["stellar"]["confidence"] == "possible"
        assert "Confidence: possible" in provider.prompts[-1]

    def test_detection_failure_does_not_crash(self, monkeypatch):
        provider = _install_provider(monkeypatch)

        def boom(rows):
            raise RuntimeError("detector exploded")

        monkeypatch.setattr(analysis, "detect_stellar_from_dicts", boom)
        result = analysis.analyze_pull_request(_pr(), _soroban_pr_files())
        assert result["stellar"]["detected"] is False
        assert result["analysis"] == "mock analysis response"
        assert "Stellar/Soroban project context" not in provider.prompts[-1]

    def test_empty_files_is_generic(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_pull_request(_pr(), [])
        assert result["stellar"]["detected"] is False
        assert result["stellar"]["confidence"] is None
        assert "no file-level diff available" in provider.prompts[-1]

    def test_ai_failure_handled(self, monkeypatch):
        from app.services.llm import LLMProviderError

        class _BrokenProvider:
            def complete(self, messages, *, stream=False):
                raise LLMProviderError("provider down")

        monkeypatch.setattr(analysis, "get_provider", lambda: _BrokenProvider())
        result = analysis.analyze_pull_request(_pr(), _soroban_pr_files())
        assert result["analysis"].startswith("[analysis unavailable")
        assert result["stellar"]["detected"] is True

    def test_repo_files_merge_with_diff(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        # Diff alone is a plain file; repo context carries the Soroban manifest.
        result = analysis.analyze_pull_request(
            _pr(), _python_pr_files(), repo_files=_soroban_repo_files()
        )
        assert result["stellar"]["detected"] is True
        assert "Stellar/Soroban project context" in provider.prompts[-1]

    def test_context_is_bounded(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        files = _soroban_pr_files()
        big = {
            "filename": "contracts/big/src/lib.rs",
            "status": "modified",
            "additions": 100000,
            "deletions": 0,
            "patch": "+" + ("fn x() {}\n" * 50000),
        }
        files.append(big)
        result = analysis.analyze_pull_request(_pr(), files)
        prompt = provider.prompts[-1]
        assert result["analysis"] == "mock analysis response"
        assert len(prompt) < 100_000


# ---------------------------------------------------------------------------
# analyze_issue (#204)
# ---------------------------------------------------------------------------


class TestIssueStellarAware:
    def test_stellar_repo_includes_context(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_issue(_issue(), "owner", "repo", repo_files=_soroban_repo_files())
        assert result["stellar"]["detected"] is True
        assert result["stellar"]["confidence"] == "likely"
        prompt = provider.prompts[-1]
        assert "Stellar/Soroban project context" in prompt
        assert "Detection evidence:" in prompt
        assert "Do NOT invent contract" in prompt

    def test_non_stellar_repo_unchanged(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_issue(_issue(), "owner", "repo", repo_files=_python_repo_files())
        assert result["stellar"]["detected"] is False
        prompt = provider.prompts[-1]
        assert "Stellar/Soroban project context" not in prompt
        assert "Suggested implementation approach" in prompt

    def test_plain_rust_not_classified(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_issue(
            _issue(), "owner", "repo", repo_files=_plain_rust_repo_files()
        )
        assert result["stellar"]["detected"] is False
        assert "Stellar/Soroban project context" not in provider.prompts[-1]

    def test_no_repo_files_is_generic(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        result = analysis.analyze_issue(_issue(), "owner", "repo")
        assert result["stellar"]["detected"] is False
        assert result["stellar"]["confidence"] is None
        assert "Stellar/Soroban project context" not in provider.prompts[-1]

    def test_detection_failure_does_not_crash(self, monkeypatch):
        _install_provider(monkeypatch)

        def boom(rows):
            raise RuntimeError("detector exploded")

        monkeypatch.setattr(analysis, "detect_stellar_from_dicts", boom)
        result = analysis.analyze_issue(_issue(), "owner", "repo", repo_files=_soroban_repo_files())
        assert result["stellar"]["detected"] is False
        assert result["analysis"] == "mock analysis response"

    def test_possible_confidence(self, monkeypatch):
        provider = _install_provider(monkeypatch)
        repo_files = [{"path": "stellar.toml", "content": "[NETWORK_TESTNET]"}]
        result = analysis.analyze_issue(_issue(), "owner", "repo", repo_files=repo_files)
        assert result["stellar"]["detected"] is True
        assert result["stellar"]["confidence"] == "possible"
        assert "Confidence: possible" in provider.prompts[-1]

    def test_ai_failure_handled(self, monkeypatch):
        from app.services.llm import LLMProviderError

        class _BrokenProvider:
            def complete(self, messages, *, stream=False):
                raise LLMProviderError("provider down")

        monkeypatch.setattr(analysis, "get_provider", lambda: _BrokenProvider())
        result = analysis.analyze_issue(_issue(), "owner", "repo", repo_files=_soroban_repo_files())
        assert result["analysis"].startswith("[analysis unavailable")
        assert result["stellar"]["detected"] is True


# ---------------------------------------------------------------------------
# Route-level integration (authorization + repo context fetch)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data
        self.headers = {"Content-Type": "application/json"} if isinstance(data, dict) else {}
        self.content = json.dumps(data).encode() if data is not None else b""

    def json(self):
        return self._data


def _make_fake_session(script):
    ordered = sorted(script, key=lambda entry: len(entry[1]), reverse=True)

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, params=None, timeout=None, **kwargs):
            url_path = url.split("api.github.com", 1)[-1].split("?", 1)[0]
            for entry in ordered:
                if entry[0] in (method, "*") and (entry[1] == "*" or entry[1] in url_path):
                    return FakeResponse(entry[2], entry[3])
            raise AssertionError(f"Unhandled request: {method} {url_path}")

        def get(self, url, params=None, timeout=None, **kwargs):
            return self.request("GET", url, params=params, timeout=timeout)

    return FakeSession()


def _logged_in_client(client):
    client.post(
        "/auth/register",
        data={
            "username": "ghuser",
            "email": "ghuser@example.com",
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
        follow_redirects=True,
    )
    return client


def _create_account(app):
    user = User.query.filter_by(username="ghuser").first()
    account = GithubAccount(user_id=user.id, github_user_id=42, github_username="ghuser")
    account.set_access_token("gho_test_token")
    db.session.add(account)
    db.session.commit()
    return account


def _stellar_repo_script():
    def contents_text(name, text):
        return {"content": base64.b64encode(text.encode()).decode()}

    return [
        ("GET", "/repos/owner/repo/issues/1", 200, {"number": 1, "title": "bug", "body": "x"}),
        ("GET", "/repos/owner/repo", 200, {"name": "repo", "default_branch": "main"}),
        (
            "GET",
            "/repos/owner/repo/git/trees/main",
            200,
            {
                "tree": [
                    {"path": "Cargo.toml", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ],
                "truncated": False,
            },
        ),
        (
            "GET",
            "/repos/owner/repo/contents/Cargo.toml",
            200,
            contents_text("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
        ),
    ]


class TestRoutes:
    def _install(self, client, app, script, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        provider = _RecordingProvider()
        monkeypatch.setattr("app.services.analysis.get_provider", lambda: provider)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(script),
        )
        return provider

    def test_issue_analysis_stellar_repo(self, client, app, monkeypatch):
        provider = self._install(client, app, _stellar_repo_script(), monkeypatch)
        data = client.get("/github/api/repos/owner/repo/issues/1?analyze=1").get_json()
        assert data["analysis"]["stellar"]["detected"] is True
        assert "Stellar/Soroban project context" in provider.prompts[-1]

    def test_issue_analysis_non_stellar_repo(self, client, app, monkeypatch):
        script = _stellar_repo_script()
        # Replace Cargo.toml content with a plain Python app.
        for i, entry in enumerate(script):
            if "/contents/Cargo.toml" in entry[1]:
                script[i] = (
                    "GET",
                    entry[1],
                    200,
                    {"content": base64.b64encode(b"APP = 'x'\n").decode()},
                )
        provider = self._install(client, app, script, monkeypatch)
        data = client.get("/github/api/repos/owner/repo/issues/1?analyze=1").get_json()
        assert data["analysis"]["stellar"]["detected"] is False
        assert "Stellar/Soroban project context" not in provider.prompts[-1]

    def test_pull_analysis_stellar_repo(self, client, app, monkeypatch):
        script = _stellar_repo_script()
        script.append(
            (
                "GET",
                "/repos/owner/repo/pulls/9",
                200,
                {"number": 9, "title": "t", "state": "open", "user": {"login": "a"}},
            )
        )
        script.append(
            (
                "GET",
                "/repos/owner/repo/pulls/9/files",
                200,
                [
                    {
                        "filename": "Cargo.toml",
                        "status": "modified",
                        "patch": "+soroban-sdk = '21.0.0'",
                    }
                ],
            )
        )
        provider = self._install(client, app, script, monkeypatch)
        data = client.get("/github/api/repos/owner/repo/pulls/9?analyze=1").get_json()
        assert data["analysis"]["stellar"]["detected"] is True
        assert "Stellar/Soroban project context" in provider.prompts[-1]

    def test_analyze_requires_connection(self, client):
        _logged_in_client(client)
        response = client.get("/github/api/repos/owner/repo/issues/1?analyze=1")
        assert response.status_code in (403, 404)
