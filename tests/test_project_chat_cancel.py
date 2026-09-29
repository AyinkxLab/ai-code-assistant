"""Cancel an in-flight project chat (issue #101).

Unlike the general chat stream (#11, which keeps the partial reply), the project
chat stream must **discard** a cancelled reply: when the client aborts the SSE
stream, no partial assistant message is persisted. The user message is kept so
the conversation stays usable.
"""

import pytest

from app.extensions import db
from app.models import Project, ProjectMessage, User, Workspace
from app.services.providers import register_provider, unregister_provider
from app.services.providers.base import LLMProvider, ProviderResponse


class _StreamingProvider(LLMProvider):
    """Yields many chunks so a test can cancel after the first few."""

    name = "proj-cancel"
    models = ("proj-cancel-1",)

    def chat(self, messages, *, model=None, params=None):
        return ProviderResponse(content="complete", model=self.models[0])

    def stream(self, messages, *, model=None, params=None):
        for index in range(50):
            yield f"part{index} "


@pytest.fixture()
def streaming_provider(monkeypatch):
    register_provider("proj-cancel", _StreamingProvider)
    monkeypatch.setenv("LLM_PROVIDER", "proj-cancel")
    yield
    unregister_provider("proj-cancel")


def _register(client):
    client.post(
        "/auth/register",
        data={
            "username": "canceller",
            "email": "canceller@example.com",
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _project():
    user = User.query.filter_by(username="canceller").first()
    workspace = Workspace(user_id=user.id, name="Cancel workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Cancel project",
        source="archive",
        status="ready",
        file_count=0,
        total_size_bytes=0,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _assistant_messages(project_id):
    return ProjectMessage.query.filter_by(project_id=project_id, role="assistant").all()


class TestProjectChatCancel:
    def test_cancel_discards_partial_assistant_message(self, client, db, streaming_provider):
        _register(client)
        project = _project()

        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat/stream",
            json={"content": "write a lot"},
            buffered=False,
        )
        iterator = response.response
        first = next(iterator)  # first SSE frame
        assert b"part0" in first
        iterator.close()  # simulate the client aborting (Stop button)

        db.session.expire_all()
        # The cancelled reply is discarded - no partial assistant message.
        assert _assistant_messages(project.id) == []
        # The user message is kept, so the conversation is still usable.
        assert ProjectMessage.query.filter_by(project_id=project.id, role="user").count() == 1

    def test_completed_stream_persists_the_full_reply(self, client, db, streaming_provider):
        _register(client)
        project = _project()

        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat/stream",
            json={"content": "hello"},
        )
        body = response.get_data(as_text=True)
        assert '"type": "done"' in body

        db.session.expire_all()
        assistant = _assistant_messages(project.id)
        assert len(assistant) == 1
        assert assistant[0].content.startswith("part0")
        assert "part49" in assistant[0].content
