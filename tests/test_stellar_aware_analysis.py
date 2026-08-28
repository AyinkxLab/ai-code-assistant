"""Tests for Stellar-aware issue and PR analysis (#203, #204).

Verifies that:
- A Stellar/Soroban PR receives the Stellar-aware review section.
- A non-Stellar PR receives the existing generic review unchanged.
- Detection failure is handled safely (falls back to generic, no crash).
- AI failure does not change review behavior (existing error handling).
- A Stellar/Soroban project's issue receives Stellar-aware analysis.
- A non-Stellar project's issue does not receive Stellar-specific analysis.
- Detection uncertainty falls back to generic analysis.
- The _PRFileAdapter and _adapt_pr_files adapter work correctly.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services import analysis
from app.services.analysis import (
    _FileAdapter,
    _adapt_pr_files,
    _adapt_repo_files,
    analyze_issue,
    analyze_pull_request,
)
from app.services.llm import LLMProviderError
from app.services.stellar_detection import detect_stellar_project


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A Soroban PR diff that touches a Cargo.toml adding soroban-sdk and a Rust
# contract source file with #[contractimpl].
_SOROBAN_PR_FILES = [
    {
        "filename": "Cargo.toml",
        "status": "modified",
        "additions": 2,
        "deletions": 0,
        "patch": (
            "--- a/Cargo.toml\n"
            "+++ b/Cargo.toml\n"
            "@@ -10,3 +10,5 @@\n"
            " [dependencies]\n"
            "+soroban-sdk = { version = \"21.0.0\" }\n"
            "+soroban-token-sdk = { version = \"21.0.0\" }\n"
        ),
    },
    {
        "filename": "src/token.rs",
        "status": "added",
        "additions": 15,
        "deletions": 0,
        "patch": (
            "--- /dev/null\n"
            "+++ b/src/token.rs\n"
            "@@ -0,0 +1,15 @@\n"
            "+#![no_std]\n"
            "+use soroban_sdk::contractimpl;\n"
            "+\n"
            "+pub struct TokenContract;\n"
            "+\n"
            "+#[contractimpl]\n"
            "+impl TokenContract {\n"
            "+    pub fn mint(env: &soroban_sdk::Env, to: soroban_sdk::Address, amount: i128) {\n"
            "+        env.storage().persistent().set(&to, &amount);\n"
            "+    }\n"
            "+}\n"
        ),
    },
]

# A non-Stellar PR diff — a simple Python change.
_GENERIC_PR_FILES = [
    {
        "filename": "app/views.py",
        "status": "modified",
        "additions": 5,
        "deletions": 2,
        "patch": (
            "--- a/app/views.py\n"
            "+++ b/app/views.py\n"
            "@@ -20,8 +20,11 @@\n"
            "-def index():\n"
            "-    return render_template('index.html')\n"
            "+def index():\n"
            "+    page = request.args.get('page', 1)\n"
            "+    items = Item.query.paginate(page=page, per_page=20)\n"
            "+    return render_template('index.html', items=items)\n"
        ),
    },
]

# Repo file list with Soroban signals for issue analysis.
_SOROBAN_REPO_FILES = [
    {"path": "Cargo.toml", "content": "[dependencies]\nsoroban-sdk = '21.0.0'\n"},
    {"path": "src/lib.rs", "content": "#![no_std]\n#[contractimpl]\npub struct Contract {}\n"},
]

# Non-Stellar repo file list.
_GENERIC_REPO_FILES = [
    {"path": "app.py", "content": "from flask import Flask\napp = Flask(__name__)\n"},
    {"path": "requirements.txt", "content": "flask==3.0.0\n"},
]


def _capture_prompt(fn, *args, **kwargs) -> str:
    """Call an analysis function and return the prompt that was sent to the provider."""
    captured: list[str] = []

    class _CaptureProvider:
        def complete(self, messages, *, stream=False):
            for msg in messages:
                if msg.get("role") == "user":
                    captured.append(msg["content"])
            return "mock analysis response"

    with patch("app.services.analysis.get_provider", return_value=_CaptureProvider()):
        fn(*args, **kwargs)

    return captured[0] if captured else ""


# ---------------------------------------------------------------------------
# Tests: _PRFileAdapter and _adapt_pr_files (#203)
# ---------------------------------------------------------------------------


class TestPRFileAdapter:
    """Adapter tests converting GitHub PR file dicts into detection inputs."""

    def test_adapt_pr_files_basic(self):
        adapters = _adapt_pr_files(
            [{"filename": "src/lib.rs", "patch": "+use soroban_sdk::contractimpl;"}]
        )
        assert len(adapters) == 1
        assert adapters[0].path == "src/lib.rs"
        assert "soroban_sdk" in adapters[0].content

    def test_adapt_pr_files_missing_filename_skipped(self):
        adapters = _adapt_pr_files([{"patch": "diff"}, {"filename": "ok.rs", "patch": "x"}])
        assert len(adapters) == 1
        assert adapters[0].path == "ok.rs"

    def test_adapt_pr_files_none_patch(self):
        adapters = _adapt_pr_files([{"filename": "binary.bin", "patch": None}])
        assert len(adapters) == 1
        assert adapters[0].path == "binary.bin"
        assert adapters[0].content is None

    def test_adapt_pr_files_empty(self):
        assert _adapt_pr_files([]) == []

    def test_adapter_path_and_content_attrs(self):
        adapter = _FileAdapter("Cargo.toml", "[dependencies]\nsoroban-sdk='1'\n")
        assert hasattr(adapter, "path")
        assert hasattr(adapter, "content")
        assert adapter.path == "Cargo.toml"

    def test_adapter_works_with_detect_stellar_project(self):
        """The adapter objects must be directly consumable by detect_stellar_project."""
        adapters = _adapt_pr_files(_SOROBAN_PR_FILES)
        signals = detect_stellar_project(adapters)
        assert signals.is_stellar
        assert signals.is_soroban
        assert signals.confidence == "likely"

    def test_adapt_repo_files_dict_form(self):
        adapters = _adapt_repo_files(
            [{"path": "Cargo.toml", "content": "soroban-sdk"}]
        )
        assert len(adapters) == 1
        assert adapters[0].path == "Cargo.toml"

    def test_adapt_repo_files_tuple_form(self):
        adapters = _adapt_repo_files([("Cargo.toml", "soroban-sdk")])
        assert len(adapters) == 1
        assert adapters[0].path == "Cargo.toml"
        assert "soroban" in adapters[0].content

    def test_adapt_repo_files_mixed(self):
        adapters = _adapt_repo_files(
            [{"path": "a.rs", "content": "#[contractimpl]"}, ("b.txt", "hello")]
        )
        assert len(adapters) == 2


# ---------------------------------------------------------------------------
# Tests: analyze_pull_request Stellar-awareness (#203)
# ---------------------------------------------------------------------------


class TestStellarAwarePRAnalysis:
    """Tests for Stellar-aware PR analysis (#203)."""

    def test_soroban_pr_includes_stellar_section(self):
        """A Stellar/Soroban PR receives the Stellar-aware review section."""
        prompt = _capture_prompt(
            analyze_pull_request,
            {"number": 1, "title": "Add token contract", "state": "open", "body": "new contract"},
            _SOROBAN_PR_FILES,
        )
        assert "Stellar/Soroban review (detected)" in prompt
        assert "authorization" in prompt.lower()
        assert "extend_ttl" in prompt
        assert "[CONFIRMED]" in prompt

    def test_non_stellar_pr_excludes_stellar_section(self):
        """A non-Stellar PR must produce the generic review unchanged."""
        prompt = _capture_prompt(
            analyze_pull_request,
            {"number": 2, "title": "Add pagination", "state": "open", "body": "paginate index"},
            _GENERIC_PR_FILES,
        )
        assert "Stellar/Soroban review" not in prompt
        assert "[CONFIRMED]" in prompt

    def test_stellar_detected_flag_true_for_soroban(self):
        """The return dict should report stellar_detected=True for Soroban PRs."""
        with patch("app.services.analysis.get_provider") as mock:
            mock.return_value.complete.return_value = "response"
            result = analyze_pull_request(
                {"number": 1, "title": "t", "state": "open", "body": "b"},
                _SOROBAN_PR_FILES,
            )
        assert result["stellar_detected"] is True

    def test_stellar_detected_flag_false_for_generic(self):
        """The return dict should report stellar_detected=False for non-Stellar PRs."""
        with patch("app.services.analysis.get_provider") as mock:
            mock.return_value.complete.return_value = "response"
            result = analyze_pull_request(
                {"number": 2, "title": "t", "state": "open", "body": "b"},
                _GENERIC_PR_FILES,
            )
        assert result["stellar_detected"] is False

    def test_detection_failure_falls_back_to_generic(self):
        """If detection raises, the PR review must still work (generic, no crash)."""
        with patch("app.services.analysis.detect_stellar_project", side_effect=RuntimeError("boom")):
            prompt = _capture_prompt(
                analyze_pull_request,
                {"number": 3, "title": "t", "state": "open", "body": "b"},
                _SOROBAN_PR_FILES,
            )
        assert "Stellar/Soroban review" not in prompt  # detection failed -> generic

    def test_ai_failure_returns_error_message(self):
        """When the LLM provider fails, the analysis field should report unavailable."""
        with patch("app.services.analysis.get_provider") as mock:
            mock.return_value.complete.side_effect = LLMProviderError("API down")
            result = analyze_pull_request(
                {"number": 4, "title": "t", "state": "open", "body": "b"},
                _SOROBAN_PR_FILES,
            )
        assert "analysis unavailable" in result["analysis"]

    def test_empty_files_generic_review(self):
        """PR with no files at all gets the generic review."""
        prompt = _capture_prompt(
            analyze_pull_request,
            {"number": 5, "title": "empty", "state": "open", "body": "b"},
            [],
        )
        assert "Stellar/Soroban review" not in prompt
        assert "(no file-level diff available)" in prompt

    def test_stellar_config_file_triggers_section(self):
        """A PR that adds a stellar.toml file triggers detection (possible confidence)."""
        files = [
            {
                "filename": "stellar.toml",
                "status": "added",
                "additions": 5,
                "deletions": 0,
                "patch": "--- /dev/null\n+++ b/stellar.toml\n@@ -0,0 +1,5 @@\n+NETWORK_PASSPHRASE=\"Test SDF Network ; September 2015\"\n+HORIZON_URL=\"https://horizon-testnet.stellar.org\"\n",
            }
        ]
        prompt = _capture_prompt(
            analyze_pull_request,
            {"number": 6, "title": "add config", "state": "open", "body": "b"},
            files,
        )
        assert "Stellar/Soroban review" in prompt


# ---------------------------------------------------------------------------
# Tests: analyze_issue Stellar-awareness (#204)
# ---------------------------------------------------------------------------


class TestStellarAwareIssueAnalysis:
    """Tests for Stellar-aware issue analysis (#204)."""

    def test_stellar_repo_issue_includes_stellar_section(self):
        """A Stellar/Soroban project's issue receives Stellar-aware analysis."""
        prompt = _capture_prompt(
            analyze_issue,
            {"number": 1, "title": "Contract bug", "body": "storage issue", "state": "open", "labels": []},
            "stellar-org",
            "soroban-token",
            repo_files=_SOROBAN_REPO_FILES,
        )
        assert "Stellar/Soroban context (detected)" in prompt
        assert "Soroban SDK" in prompt
        assert "XDR" in prompt
        assert "[CONFIRMED]" in prompt

    def test_non_stellar_repo_issue_excludes_stellar_section(self):
        """A non-Stellar project's issue does not receive Stellar-specific analysis."""
        prompt = _capture_prompt(
            analyze_issue,
            {"number": 2, "title": "Fix pagination", "body": "paginate items", "state": "open", "labels": []},
            "some-user",
            "flask-app",
            repo_files=_GENERIC_REPO_FILES,
        )
        assert "Stellar/Soroban context" not in prompt
        assert "structured analysis" in prompt

    def test_no_repo_files_generic_analysis(self):
        """When no repo_files is provided, the generic issue analysis is used."""
        prompt = _capture_prompt(
            analyze_issue,
            {"number": 3, "title": "Bug", "body": "desc", "state": "open", "labels": []},
            "owner",
            "repo",
        )
        assert "Stellar/Soroban context" not in prompt

    def test_repo_files_none_generic_analysis(self):
        """repo_files=None should also produce generic analysis."""
        prompt = _capture_prompt(
            analyze_issue,
            {"number": 4, "title": "Bug", "body": "desc", "state": "open", "labels": []},
            "owner",
            "repo",
            repo_files=None,
        )
        assert "Stellar/Soroban context" not in prompt

    def test_empty_repo_files_generic_analysis(self):
        """Empty repo_files list should produce generic analysis."""
        prompt = _capture_prompt(
            analyze_issue,
            {"number": 5, "title": "Bug", "body": "desc", "state": "open", "labels": []},
            "owner",
            "repo",
            repo_files=[],
        )
        assert "Stellar/Soroban context" not in prompt

    def test_detection_uncertainty_falls_back(self):
        """Low-confidence detection (no signals) must not produce Stellar claims."""
        uncertain_files = [{"path": "README.md", "content": "# some project that mentions stellar in passing"}]
        prompt = _capture_prompt(
            analyze_issue,
            {"number": 6, "title": "Bug", "body": "desc", "state": "open", "labels": []},
            "owner",
            "repo",
            repo_files=uncertain_files,
        )
        assert "Stellar/Soroban context" not in prompt

    def test_detection_failure_falls_back_to_generic(self):
        """If detection raises, the issue analysis must still work (generic, no crash)."""
        with patch("app.services.analysis.detect_stellar_project", side_effect=RuntimeError("fail")):
            prompt = _capture_prompt(
                analyze_issue,
                {"number": 7, "title": "Bug", "body": "desc", "state": "open", "labels": []},
                "owner",
                "repo",
                repo_files=_SOROBAN_REPO_FILES,
            )
        assert "Stellar/Soroban context" not in prompt

    def test_ai_failure_returns_error_message(self):
        """When the LLM provider fails, the analysis field should report unavailable."""
        with patch("app.services.analysis.get_provider") as mock:
            mock.return_value.complete.side_effect = LLMProviderError("API down")
            result = analyze_issue(
                {"number": 8, "title": "Bug", "body": "desc", "state": "open", "labels": []},
                "owner",
                "repo",
                repo_files=_SOROBAN_REPO_FILES,
            )
        assert "analysis unavailable" in result["analysis"]

    def test_stellar_issue_with_tuple_form_files(self):
        """repo_files as tuples should also work for detection."""
        tuple_files = [("Cargo.toml", "[dependencies]\nsoroban-sdk='1'\n")]
        prompt = _capture_prompt(
            analyze_issue,
            {"number": 9, "title": "Bug", "body": "desc", "state": "open", "labels": []},
            "owner",
            "repo",
            repo_files=tuple_files,
        )
        assert "Stellar/Soroban context" in prompt

    def test_issue_return_dict_structure(self):
        """Return dict must have the expected keys."""
        with patch("app.services.analysis.get_provider") as mock:
            mock.return_value.complete.return_value = "analysis text"
            result = analyze_issue(
                {"number": 10, "title": "T", "body": "B", "state": "open", "labels": ["bug"]},
                "owner",
                "repo",
            )
        assert result["kind"] == "issue"
        assert result["issue_number"] == 10
        assert result["title"] == "T"
        assert result["analysis"] == "analysis text"


# ---------------------------------------------------------------------------
# Tests: backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Ensure the changes do not break existing callers."""

    def test_analyze_issue_without_repo_files_works(self):
        """analyze_issue must still work when repo_files is not passed (backward compat)."""
        with patch("app.services.analysis.get_provider") as mock:
            mock.return_value.complete.return_value = "analysis"
            result = analyze_issue(
                {"number": 1, "title": "T", "body": "B", "state": "open", "labels": []},
                "owner",
                "repo",
            )
        assert result["analysis"] == "analysis"
        assert result["kind"] == "issue"

    def test_analyze_pull_request_return_has_expected_keys(self):
        """The PR analysis return dict should contain the expected keys."""
        with patch("app.services.analysis.get_provider") as mock:
            mock.return_value.complete.return_value = "analysis"
            result = analyze_pull_request(
                {"number": 1, "title": "T", "state": "open", "body": "B", "merged": False},
                _GENERIC_PR_FILES,
            )
        assert result["kind"] == "pull_request"
        assert result["pr_number"] == 1
        assert result["title"] == "T"
        assert result["analysis"] == "analysis"
        assert "stellar_detected" in result
