"""The Stellar/Soroban analysis prompt must request the contract-specific
sections (issue #186): entry points, storage keys, cross-contract calls, and
missing tests, each labelled [CONFIRMED] / [SUGGESTION]."""

import contextlib

from flask_login import login_user

from app.extensions import db
from app.models import Project, ProjectFile, User, Workspace
from app.services import project_analysis


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _soroban_project(owner):
    workspace = Workspace(user_id=owner.id, name="Stellar workspace")
    db.session.add(workspace)
    db.session.commit()
    files = [
        ("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
        (
            "src/lib.rs",
            "#![no_std]\n#[contractimpl]\npub struct TokenContract {}\n",
        ),
    ]
    project = Project(
        workspace_id=workspace.id,
        user_id=owner.id,
        name="Token",
        source="archive",
        status="ready",
        file_count=len(files),
        total_size_bytes=sum(len(c) for _, c in files),
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


def test_stellar_prompt_requests_contract_sections(app, make_user, monkeypatch):
    owner = make_user(username="owner", email="owner@example.com")
    project = _soroban_project(owner)

    captured: dict[str, str] = {}

    def fake_run(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(project_analysis, "_run", fake_run)

    with _authorized_context(app, owner):
        result = project_analysis.analyze_stellar_project(project)

    assert result["analysis"] == "ok"
    prompt = captured["prompt"].lower()

    for marker in (
        "entry points",
        "#[contractimpl]",
        "storage keys",
        "cross-contract",
        "missing tests",
        "[confirmed]",
        "[suggestion]",
    ):
        assert marker in prompt, f"prompt missing: {marker}"

    # The grounding / fail-closed instructions are preserved.
    assert "base every statement on the files shown" in prompt
    assert "do not demonstrate" in prompt.lower()


def test_non_stellar_project_does_not_invoke_prompt(app, make_user, monkeypatch):
    owner = make_user(username="owner2", email="owner2@example.com")
    workspace = Workspace(user_id=owner.id, name="w")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=owner.id,
        name="py",
        source="archive",
        status="ready",
        file_count=1,
        total_size_bytes=10,
    )
    db.session.add(project)
    db.session.commit()
    db.session.add(
        ProjectFile(
            project_id=project.id,
            path="main.py",
            size=10,
            is_binary=False,
            language="python",
            content="print('hi')",
        )
    )
    db.session.commit()

    def fake_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("prompt must not run for a non-Stellar project")

    monkeypatch.setattr(project_analysis, "_run", fake_run)

    with _authorized_context(app, owner):
        result = project_analysis.analyze_stellar_project(project)
    assert result["detected"] is False
