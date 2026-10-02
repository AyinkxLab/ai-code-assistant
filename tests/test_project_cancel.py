"""Cancelling an in-flight project chat stream or analysis (issue #101).

Covers the cancel endpoint, the SSE stream aborting without persisting a
partial assistant message, and the non-streaming analysis skipping its side
effects when cancelled. ``_setup`` mirrors the project fixture used by
``test_project_routes``.
"""

import pytest

from app.extensions import db
from app.models import ActivityEvent, Project, ProjectFile, ProjectMessage, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services import cancellation


def _setup(app, make_user, login, username="projuser", email="projuser@example.com"):
    user = make_user(username=username, email=email)
    login(email=email)
    workspace = Workspace(user_id=user.id, name="Project workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Demo",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
        file_count=2,
        total_size_bytes=12,
        indexed_at=db.func.now(),
    )
    db.session.add(project)
    db.session.commit()
    db.session.add_all(
        [
            ProjectFile(
                project_id=project.id,
                path="app.py",
                size=8,
                is_binary=False,
                language="python",
                content="print(1)",
            ),
            ProjectFile(
                project_id=project.id,
                path="README.md",
                size=4,
                is_binary=False,
                language="markdown",
                content="# Hi",
            ),
        ]
    )
    db.session.commit()
    return workspace, project


@pytest.fixture(autouse=True)
def _reset_cancellation():
    cancellation.reset()
    yield
    cancellation.reset()


def _roles(app, project):
    with app.app_context():
        return [
            message.role
            for message in ProjectMessage.query.filter_by(project_id=project.id)
            .order_by(ProjectMessage.created_at)
            .all()
        ]


def test_cancel_requires_request_id(client, app, make_user, login):
    _, project = _setup(app, make_user, login)
    response = client.post(f"/workspaces/api/projects/{project.id}/cancel", json={})
    assert response.status_code == 400


def test_cancel_marks_the_request_for_analysis(client, app, make_user, login):
    _, project = _setup(app, make_user, login)
    assert (
        client.post(
            f"/workspaces/api/projects/{project.id}/cancel", json={"request_id": "req-1"}
        ).status_code
        == 200
    )
    assert cancellation.is_cancelled("req-1") is True

    # The analysis sees the cancellation and reports it without side effects.
    response = client.post(
        f"/workspaces/api/projects/{project.id}/analyze",
        json={"kind": "bugs", "request_id": "req-1"},
    )
    assert response.status_code == 200
    assert response.get_json()["cancelled"] is True


def test_cancel_is_owner_scoped(client, app, make_user, login):
    _, project = _setup(app, make_user, login)
    make_user(username="intruder", email="intruder@example.com")
    login(email="intruder@example.com")
    response = client.post(
        f"/workspaces/api/projects/{project.id}/cancel", json={"request_id": "x"}
    )
    assert response.status_code == 404


def test_cancelled_stream_does_not_persist_partial_reply(client, app, make_user, login):
    _, project = _setup(app, make_user, login)
    client.post(f"/workspaces/api/projects/{project.id}/cancel", json={"request_id": "stream-1"})

    response = client.post(
        f"/workspaces/api/projects/{project.id}/chat/stream",
        json={"content": "Explain app.py", "request_id": "stream-1"},
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '"type": "done"' not in body
    # Only the user's message was persisted; no partial assistant message.
    assert _roles(app, project) == ["user"]


def test_stream_completion_persists_the_assistant_reply(client, app, make_user, login):
    _, project = _setup(app, make_user, login)
    response = client.post(
        f"/workspaces/api/projects/{project.id}/chat/stream",
        json={"content": "Explain app.py", "request_id": "stream-2"},
    )

    assert response.status_code == 200
    assert '"type": "done"' in response.get_data(as_text=True)
    assert _roles(app, project) == ["user", "assistant"]


def test_analysis_without_cancel_runs_and_records_activity(client, app, make_user, login):
    _, project = _setup(app, make_user, login)
    response = client.post(f"/workspaces/api/projects/{project.id}/analyze", json={"kind": "bugs"})
    assert response.status_code == 200
    assert response.get_json()["kind"] == "bugs"
    with app.app_context():
        assert ActivityEvent.query.count() >= 1


def test_cancelled_analysis_skips_side_effects(client, app, make_user, login):
    _, project = _setup(app, make_user, login)
    client.post(f"/workspaces/api/projects/{project.id}/cancel", json={"request_id": "ana-1"})

    response = client.post(
        f"/workspaces/api/projects/{project.id}/analyze",
        json={"kind": "bugs", "request_id": "ana-1"},
    )

    assert response.status_code == 200
    assert response.get_json()["cancelled"] is True
    with app.app_context():
        assert ActivityEvent.query.count() == 0
