"""Tests for the Stellar network-configuration review analysis (#189).

Verifies the config review is gated on a detected Stellar/Soroban project,
returns an honest result when no config files exist, and that the prompt is
grounded strictly in the detected configuration files (marking findings
``[SUGGESTION]`` unless directly evident).
"""

import contextlib

from flask_login import login_user

from app.extensions import db
from app.models import Project, ProjectFile, User, Workspace, WorkspaceMember
from app.services import project_analysis
from app.services.project_analysis import analyze_stellar_config


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _project(owner, files):
    workspace = Workspace(user_id=owner.id, name="Stellar workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=owner.id,
        name="Stellar project",
        source="archive",
        status="ready",
        file_count=len(files),
        total_size_bytes=sum(len(c or "") for _, c in files),
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
                language="rust",
                content=content,
            )
        )
    db.session.commit()
    return project


@contextlib.contextmanager
def _authorized_context(app, user):
    with app.test_request_context("/"):
        login_user(user)
        yield


def _capture_prompt(monkeypatch):
    captured = {}

    def fake_run(prompt, **_kwargs):
        captured["prompt"] = prompt
        return "configuration review complete"

    monkeypatch.setattr(project_analysis, "_run", fake_run)
    return captured


SOROBAN_CARGO = "[package]\nname='tokens'\n[dependencies]\nsoroban-sdk = '21.0.0'\n"
MISCONFIGURED_CONFIG = (
    'network = "testnet"\n'
    'passphrase = "Test SDF Network ; September 2015"\n'
    'rpc_url = "https://soroban-mainnet.stellar.org"\n'
)


class TestStellarConfigGating:
    def test_kind_registered(self):
        assert "stellar_config" in project_analysis.ANALYSIS_KINDS

    def test_non_stellar_project_honest_answer(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("src/main.py", "print('hello')")])
        with _authorized_context(app, owner):
            result = analyze_stellar_config(project)
            assert result["kind"] == "stellar_config"
            assert result["detected"] is False
            assert "not applicable" in result["analysis"].lower()

    def test_detected_from_config_file_only(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("stellar.toml", "network = 'testnet'\n")])
        with _authorized_context(app, owner):
            result = analyze_stellar_config(project)
            assert result["detected"] is True
            assert result["config_files"] == ["stellar.toml"]

    def test_member_fails_closed(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        project = _project(owner, [("stellar.toml", "network = 'testnet'\n")])
        db.session.add(
            WorkspaceMember(
                workspace_id=project.workspace_id,
                user_id=member.id,
                role="viewer",
            )
        )
        db.session.commit()
        with _authorized_context(app, member):
            raised = False
            try:
                analyze_stellar_config(project)
            except Exception:
                raised = True
        assert raised

    def test_dispatches_via_analyze_project(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("stellar.toml", "network = 'testnet'\n")])
        with _authorized_context(app, owner):
            result = project_analysis.analyze_project(project, "stellar_config")
            assert result["kind"] == "stellar_config"
            assert "detected" in result


class TestStellarConfigContent:
    def test_prompt_reviews_consistency_and_marks_suggestions(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(
            owner,
            [("Cargo.toml", SOROBAN_CARGO), ("stellar.toml", MISCONFIGURED_CONFIG)],
        )
        captured = _capture_prompt(monkeypatch)
        with _authorized_context(app, owner):
            result = analyze_stellar_config(project)
        prompt = captured["prompt"]
        assert "consistency" in prompt
        assert "misconfiguration" in prompt
        assert "[SUGGESTION]" in prompt
        assert "mainnet" in prompt
        # Grounded in the detected config file.
        assert "stellar.toml" in prompt
        assert "soroban-mainnet.stellar.org" in prompt
        assert result["config_files"] == ["stellar.toml"]

    def test_prompt_excludes_non_config_files(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(
            owner,
            [
                ("Cargo.toml", SOROBAN_CARGO),
                ("src/lib.rs", "#[contractimpl]\n// UNIQUE_CONTRACT_MARKER\n"),
                ("stellar.toml", "network = 'testnet'\n# UNIQUE_CONFIG_MARKER\n"),
            ],
        )
        captured = _capture_prompt(monkeypatch)
        with _authorized_context(app, owner):
            analyze_stellar_config(project)
        prompt = captured["prompt"]
        assert "UNIQUE_CONFIG_MARKER" in prompt
        assert "UNIQUE_CONTRACT_MARKER" not in prompt

    def test_detected_without_config_files_is_honest(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(
            owner,
            [("Cargo.toml", SOROBAN_CARGO), ("src/lib.rs", "#[contractimpl]\n")],
        )
        captured = _capture_prompt(monkeypatch)
        with _authorized_context(app, owner):
            result = analyze_stellar_config(project)
        assert result["detected"] is True
        assert result["config_files"] == []
        assert "no Stellar configuration files" in result["analysis"]
        assert "prompt" not in captured  # no model call when there is nothing to review

    def test_reports_configured_network_hint(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(
            owner,
            [
                ("Cargo.toml", SOROBAN_CARGO),
                ("stellar.toml", 'passphrase = "Test SDF Network ; September 2015"\n'),
            ],
        )
        _capture_prompt(monkeypatch)
        with _authorized_context(app, owner):
            result = analyze_stellar_config(project)
        assert result["network"]["network"] == "testnet"
        assert result["is_soroban"] is True
