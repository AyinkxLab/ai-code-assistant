"""Tests for the Stellar/Soroban AI analysis and its authorization boundaries.

Verifies that the analysis only ever runs against projects the requesting user
owns (fail closed), that non-Stellar projects are reported honestly (never
fabricated), and that Stellar projects produce a grounded analysis.
"""

import contextlib

from flask_login import login_user

from app.extensions import db
from app.models import Project, ProjectFile, User, Workspace, WorkspaceMember
from app.services import project_analysis
from app.services.project_analysis import analyze_stellar_project


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


class TestStellarAnalysisDetection:
    def test_non_stellar_project_honest_answer(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("src/main.py", "print('hello')"), ("README.md", "# app")])
        with _authorized_context(app, owner):
            result = analyze_stellar_project(project)
            assert result["kind"] == "stellar"
            assert result["detected"] is False
            assert "not applicable" in result["analysis"].lower()

    def test_soroban_project_analyzed(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(
            owner,
            [
                (
                    "Cargo.toml",
                    "[package]\nname='tokens'\n[dependencies]\n"
                    "soroban-sdk = { version = '21.0.0' }\n",
                ),
                ("src/lib.rs", "#![no_std]\n#[contractimpl]\npub struct TokenContract {}\n"),
            ],
        )
        with _authorized_context(app, owner):
            result = analyze_stellar_project(project)
            assert result["kind"] == "stellar"
            assert result["detected"] is True
            assert result["confidence"] == "likely"
            assert result["is_soroban"] is True
            assert len(result["analysis"]) > 0

    def test_analysis_kind_registered(self):
        assert "stellar" in project_analysis.ANALYSIS_KINDS

    def test_analyze_project_dispatches_stellar_kind(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("Cargo.toml", "[dependencies]\nsoroban-sdk='21.0.0'\n")])
        with _authorized_context(app, owner):
            result = project_analysis.analyze_project(project, "stellar")
            assert result["kind"] == "stellar"
            assert "detected" in result


class TestAuthorizationBoundaries:
    def test_member_cannot_analyze_owners_project(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        project = _project(owner, [("Cargo.toml", "[dependencies]\nsoroban-sdk='21.0.0'\n")])
        db.session.add(
            WorkspaceMember(
                workspace_id=project.workspace_id,
                user_id=member.id,
                role="viewer",
            )
        )
        db.session.commit()
        with _authorized_context(app, member):
            try:
                analyze_stellar_project(project)
                raised = False
            except Exception:
                raised = True
        assert raised

    def test_unauthenticated_fails_closed(self, app):
        owner = _create_user("o2", "o2@example.com")
        project = _project(owner, [("Cargo.toml", "[dependencies]\nsoroban-sdk='1.0.0'\n")])
        with app.test_request_context("/"):
            try:
                analyze_stellar_project(project)
                raised = False
            except Exception:
                raised = True
        assert raised
