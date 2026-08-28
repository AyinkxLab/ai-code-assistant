"""Tests for the strengthened Stellar/Soroban AI analysis."""

import contextlib

from flask_login import login_user

from app.extensions import db
from app.models import Project, ProjectFile, User, Workspace
from app.services import project_analysis
from app.services.project_analysis import (
    analyze_stellar_project,
    analyze_stellar_security,
    stellar_analysis_context,
)


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


class TestStellarSecurityAnalysis:
    def test_kind_registered(self):
        assert "stellar_security" in project_analysis.ANALYSIS_KINDS

    def test_non_stellar_honest_answer(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("src/main.py", "print('hi')")])
        with _authorized_context(app, owner):
            result = analyze_stellar_security(project)
            assert result["kind"] == "stellar_security"
            assert result["detected"] is False
            assert "not applicable" in result["analysis"].lower()

    def test_soroban_project_analyzed(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(
            owner,
            [
                ("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
                ("src/lib.rs", "#![no_std]\n#[contractimpl]\npub struct C {}\n"),
            ],
        )
        with _authorized_context(app, owner):
            result = analyze_stellar_security(project)
            assert result["detected"] is True
            assert result["is_soroban"] is True
            assert len(result["analysis"]) > 0

    def test_dispatch_via_analyze_project(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("Cargo.toml", "[dependencies]\nsoroban-sdk='21.0.0'\n")])
        with _authorized_context(app, owner):
            result = project_analysis.analyze_project(project, "stellar_security")
            assert result["kind"] == "stellar_security"
            assert "detected" in result

    def test_member_fails_closed(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        project = _project(owner, [("Cargo.toml", "[dependencies]\nsoroban-sdk='21.0.0'\n")])
        with _authorized_context(app, member):
            raised = False
            try:
                analyze_stellar_security(project)
            except Exception:
                raised = True
        assert raised


class TestStellarAnalysisContext:
    def test_configured_network_present(self, app):
        with app.app_context():
            context = stellar_analysis_context()
        assert "testnet" in context
        assert "passphrase" in context

    def test_rpc_unavailable_is_honest(self, app, monkeypatch):
        def broken_rpc():
            raise RuntimeError("offline")

        monkeypatch.setattr("app.services.soroban_rpc.get_soroban_rpc_client", broken_rpc)
        with app.app_context():
            context = stellar_analysis_context()
        assert "unavailable" in context.lower()
        assert "do not claim" in context.lower()


class TestStellarAnalysisNetworkField:
    def test_network_hint_in_result(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(
            owner,
            [
                ("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
                (
                    "stellar.toml",
                    'passphrase = "Test SDF Network ; September 2015"\n',
                ),
            ],
        )
        with _authorized_context(app, owner):
            result = analyze_stellar_project(project)
            assert result["network"]["network"] == "testnet"

    def test_unknown_network_when_no_hint(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("Cargo.toml", "[dependencies]\nsoroban-sdk='21.0.0'\n")])
        with _authorized_context(app, owner):
            result = analyze_stellar_project(project)
            assert result["network"]["network"] is None
