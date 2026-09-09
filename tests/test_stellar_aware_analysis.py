"""Tests for Stellar-aware GitHub issue and PR analysis."""

from __future__ import annotations

from app.services import analysis
from app.services.llm import LLMProviderError


class _RecordingProvider:
    """Record the prompt sent to the LLM and return a deterministic reply."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, messages, *, stream=False):
        user_messages = [message for message in messages if message.get("role") == "user"]
        self.prompts.append(user_messages[-1]["content"])
        return "mock analysis response"

    def stream(self, messages):
        return iter(["mock analysis response"])


def _install_provider(monkeypatch) -> _RecordingProvider:
    provider = _RecordingProvider()
    monkeypatch.setattr(analysis, "get_provider", lambda: provider)
    return provider


def _pull_request() -> dict:
    return {
        "number": 8,
        "title": "Update token contract",
        "state": "open",
        "merged": False,
        "author": "alice",
        "base": "main",
        "head": "feature",
        "body": "Update the token contract authorization checks.",
    }


def _issue() -> dict:
    return {
        "number": 4,
        "title": "Missing admin check",
        "state": "open",
        "labels": ["bug"],
        "body": "The admin function has no authorization check.",
    }


_SOROBAN_PR_FILES = [
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

_GENERIC_PR_FILES = [
    {
        "filename": "app/example.py",
        "status": "modified",
        "additions": 3,
        "deletions": 1,
        "patch": "+def handler(): return 42",
    }
]

_SOROBAN_REPO_FILES = [
    {
        "path": "Cargo.toml",
        "content": "[dependencies]\nsoroban-sdk = { version = '21.0.0' }\n",
    },
    {"path": "src/lib.rs", "content": "#[contractimpl]\npub struct TokenContract {}\n"},
]

_GENERIC_REPO_FILES = [
    {"path": "Cargo.toml", "content": "[dependencies]\nserde = '1.0'\n"},
    {"path": "src/main.rs", "content": "fn main() {}\n"},
]


def test_soroban_pull_request_gets_bounded_stellar_context(monkeypatch):
    provider = _install_provider(monkeypatch)
    result = analysis.analyze_pull_request(_pull_request(), _SOROBAN_PR_FILES)

    prompt = provider.prompts[-1]
    assert result["stellar"]["detected"] is True
    assert result["stellar"]["confidence"] == "likely"
    assert "Stellar/Soroban project context" in prompt
    assert "Changed files in scope:" in prompt
    assert "Do NOT claim formal verification" in prompt


def test_plain_rust_pull_request_stays_generic(monkeypatch):
    provider = _install_provider(monkeypatch)
    result = analysis.analyze_pull_request(_pull_request(), _GENERIC_PR_FILES)

    assert result["stellar"]["detected"] is False
    assert result["stellar"]["confidence"] is None
    assert "Stellar/Soroban project context" not in provider.prompts[-1]


def test_pull_request_repo_context_can_supply_detection_evidence(monkeypatch):
    provider = _install_provider(monkeypatch)
    result = analysis.analyze_pull_request(
        _pull_request(), _GENERIC_PR_FILES, repo_files=_SOROBAN_REPO_FILES
    )

    assert result["stellar"]["detected"] is True
    assert "Stellar/Soroban project context" in provider.prompts[-1]


def test_soroban_repo_issue_gets_stellar_guidance(monkeypatch):
    provider = _install_provider(monkeypatch)
    result = analysis.analyze_issue(_issue(), "owner", "repo", repo_files=_SOROBAN_REPO_FILES)

    prompt = provider.prompts[-1]
    assert result["stellar"]["detected"] is True
    assert result["stellar"]["confidence"] == "likely"
    assert "Stellar/Soroban project context" in prompt
    assert "Do NOT invent contract behavior" in prompt


def test_non_stellar_repo_issue_stays_generic(monkeypatch):
    provider = _install_provider(monkeypatch)
    result = analysis.analyze_issue(_issue(), "owner", "repo", repo_files=_GENERIC_REPO_FILES)

    assert result["stellar"]["detected"] is False
    assert "Stellar/Soroban project context" not in provider.prompts[-1]
    assert "Suggested implementation approach" in provider.prompts[-1]


def test_missing_repo_files_stays_generic(monkeypatch):
    provider = _install_provider(monkeypatch)
    result = analysis.analyze_issue(_issue(), "owner", "repo")

    assert result["stellar"] == {"detected": False, "confidence": None}
    assert "Stellar/Soroban project context" not in provider.prompts[-1]


def test_detector_failure_falls_back_to_generic(monkeypatch):
    provider = _install_provider(monkeypatch)

    def detector_fails(rows):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(analysis, "detect_stellar_from_dicts", detector_fails)
    result = analysis.analyze_pull_request(_pull_request(), _SOROBAN_PR_FILES)

    assert result["stellar"] == {"detected": False, "confidence": None}
    assert "Stellar/Soroban project context" not in provider.prompts[-1]
    assert result["analysis"] == "mock analysis response"


def test_llm_failure_is_reported_without_losing_detection_metadata(monkeypatch):
    class _BrokenProvider:
        def complete(self, messages, *, stream=False):
            raise LLMProviderError("provider unavailable")

        def stream(self, messages):
            return iter(["provider unavailable"])

    monkeypatch.setattr(analysis, "get_provider", lambda: _BrokenProvider())
    result = analysis.analyze_pull_request(_pull_request(), _SOROBAN_PR_FILES)

    assert result["analysis"].startswith("[analysis unavailable")
    assert result["stellar"]["detected"] is True
