"""Tests for structured Stellar security findings (#181).

Covers defensive parsing/normalization of the model's JSON findings block,
severity normalization, persistence + replacement, graceful parse-failure
fallback, non-Stellar gating (no findings), and owner-scoped read access with
no cross-project leakage. Uses a mocked ``_run``; no real LLM/network calls.
"""

import contextlib

from flask_login import login_user

from app.extensions import db
from app.models import Project, ProjectFile, StellarSecurityFinding, User, Workspace
from app.services.project_analysis import analyze_stellar_security
from app.services.stellar_findings import (
    list_project_findings,
    parse_stellar_findings,
    replace_project_findings,
)

SOROBAN_FILES = [
    ("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
    ("src/lib.rs", "#![no_std]\n#[contractimpl]\npub struct C {}\n"),
]


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


def _json_response():
    return (
        "Narrative summary of the risks.\n\n"
        "```json\n"
        '{"findings": ['
        '{"severity": "critical", "category": "authorization", "file": "src/lib.rs", '
        '"line": 12, "confidence": "confirmed", '
        '"evidence": "pub fn admin(...)", '
        '"explanation": "public entrypoint lacks an auth check", '
        '"remediation": "require auth first"},'
        '{"severity": "Info", "category": "secrets", "file": "stellar.toml", '
        '"line": null, "confidence": "[SUGGESTION]", '
        '"explanation": "possible placeholder secret", '
        '"remediation": "use env vars"}'
        "]}\n"
        "```"
    )


class TestParsing:
    def test_fenced_json_block_parsed(self):
        findings = parse_stellar_findings(_json_response())
        assert len(findings) == 2
        assert findings[0]["severity"] == "critical"
        assert findings[0]["category"] == "authorization"
        assert findings[0]["file"] == "src/lib.rs"
        assert findings[0]["line"] == 12
        assert findings[0]["confidence"] == "confirmed"
        assert findings[0]["explanation"]

    def test_severity_normalization(self):
        findings = parse_stellar_findings(_json_response())
        assert findings[1]["severity"] == "informational"
        assert findings[1]["confidence"] == "suggestion"

    def test_whole_body_json_object(self):
        text = '{"findings": [{"severity": "high", "explanation": "x"}]}'
        findings = parse_stellar_findings(text)
        assert findings[0]["severity"] == "high"

    def test_malformed_returns_empty(self):
        assert parse_stellar_findings("no json here at all") == []
        assert parse_stellar_findings('{"findings": [') == []
        assert parse_stellar_findings(None) == []

    def test_unknown_values_normalized_safely(self):
        text = '{"findings": [{"severity": "blah", "category": "panic!", "explanation": "x"}]}'
        findings = parse_stellar_findings(text)
        assert findings[0]["severity"] == "medium"
        assert findings[0]["category"] == "error-handling"
        assert findings[0]["confidence"] == "suggestion"

    def test_line_parsing(self):
        text = '{"findings": [{"file": "a.rs", "line": "line 42", "explanation": "x"}]}'
        findings = parse_stellar_findings(text)
        assert findings[0]["line"] == 42

    def test_empty_findings_list(self):
        assert parse_stellar_findings('{"findings": []}') == []


class TestAnalysisIntegration:
    def test_structured_findings_returned_and_persisted(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, SOROBAN_FILES)
        monkeypatch.setattr(
            "app.services.project_analysis._run", lambda prompt, **kwargs: _json_response()
        )
        with _authorized_context(app, owner):
            result = analyze_stellar_security(project)
        assert result["detected"] is True
        assert result["structured"] is True
        assert result["findings_count"] == 2
        assert result["persisted_count"] == 2
        rows = StellarSecurityFinding.query.filter_by(project_id=project.id).all()
        assert len(rows) == 2
        assert {row.severity for row in rows} == {"critical", "informational"}

    def test_parse_failure_falls_back_to_narrative(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, SOROBAN_FILES)
        narrative = "A detailed narrative with no machine-readable block at all."
        monkeypatch.setattr(
            "app.services.project_analysis._run", lambda prompt, **kwargs: narrative
        )
        with _authorized_context(app, owner):
            result = analyze_stellar_security(project)
        assert result["analysis"] == narrative
        assert result["findings"] == []
        assert result["structured"] is False
        assert StellarSecurityFinding.query.filter_by(project_id=project.id).count() == 0

    def test_rerun_replaces_findings(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, SOROBAN_FILES)
        first = '{"findings": [{"severity": "high", "file": "a.rs", "explanation": "one"}]}'
        second = '{"findings": [{"severity": "low", "file": "b.rs", "explanation": "two"}]}'
        responses = iter([first, second])
        monkeypatch.setattr(
            "app.services.project_analysis._run",
            lambda prompt, **kwargs: next(responses),
        )
        with _authorized_context(app, owner):
            analyze_stellar_security(project)
            db.session.commit()
            analyze_stellar_security(project)
        rows = StellarSecurityFinding.query.filter_by(project_id=project.id).all()
        assert len(rows) == 1
        assert rows[0].explanation == "two"

    def test_non_stellar_project_not_persisted(self, app, make_user, monkeypatch):
        owner = make_user(username="owner", email="owner@example.com")
        project = _project(owner, [("src/main.py", "print('hi')")])
        monkeypatch.setattr(
            "app.services.project_analysis._run", lambda prompt, **kwargs: _json_response()
        )
        with _authorized_context(app, owner):
            result = analyze_stellar_security(project)
        assert result["detected"] is False
        assert result["findings"] == []
        assert StellarSecurityFinding.query.filter_by(project_id=project.id).count() == 0


class TestReadAccess:
    def _findings_url(self, project_id):
        return f"/workspaces/api/projects/{project_id}/stellar/security-findings"

    def test_owner_can_read_findings(self, app, client, make_user, login):
        owner = make_user()
        login()
        project = _project(owner, SOROBAN_FILES)
        replace_project_findings(
            project.id,
            [{"severity": "high", "category": "authorization", "explanation": "x"}],
        )
        db.session.commit()
        response = client.get(self._findings_url(project.id))
        assert response.status_code == 200
        data = response.get_json()
        assert data["count"] == 1
        assert data["findings"][0]["severity"] == "high"

    def test_non_owner_gets_404(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="outsider", email="outsider@example.com")
        project = _project(owner, SOROBAN_FILES)
        replace_project_findings(project.id, [{"severity": "medium", "explanation": "secret-ish"}])
        db.session.commit()
        login(email="outsider@example.com")
        assert client.get(self._findings_url(project.id)).status_code == 404

    def test_findings_isolated_between_projects(self, app, client, make_user, login):
        owner = make_user()
        login()
        project_a = _project(owner, SOROBAN_FILES)
        project_b = _project(owner, SOROBAN_FILES)
        replace_project_findings(project_a.id, [{"severity": "high", "explanation": "in A"}])
        replace_project_findings(project_b.id, [{"severity": "low", "explanation": "in B"}])
        db.session.commit()
        a = client.get(self._findings_url(project_a.id)).get_json()
        b = client.get(self._findings_url(project_b.id)).get_json()
        assert a["count"] == 1 and a["findings"][0]["project_id"] == project_a.id
        assert b["count"] == 1 and b["findings"][0]["project_id"] == project_b.id

    def test_list_project_findings_empty(self, app, make_user):
        owner = make_user()
        project = _project(owner, SOROBAN_FILES)
        assert list_project_findings(project.id) == []
