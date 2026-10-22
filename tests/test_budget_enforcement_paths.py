"""Hard-limit enforcement across every billable path (issue #465).

The hard limit must be enforced the same way for chat, project chat, analysis,
reviews and the AI tools. Enforcement lives in one place
(:func:`app.services.budgets.budget_guard` / ``budget_refusal``); these tests
prove every billable endpoint still routes through it and that a blocked
request never reaches the provider or writes usage.
"""

from datetime import UTC, datetime

from flask_login import login_user

from app.extensions import db
from app.models import (
    Conversation,
    Project,
    ProjectFile,
    UsageRecord,
    Workspace,
)
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services import analysis, budgets

#: Every endpoint that can invoke an LLM provider. New billable routes must be
#: added here *and* carry ``@budgets.budget_guard``.
BILLABLE_ENDPOINTS = {
    "chat.send_message",
    "chat.stream_message",
    "chat_api.send_message",
    "github.api_analyze_file",
    "github.api_analyze_repo",
    "reviews.api_create_review",
    "reviews.api_retry_review",
    "tools.analyze_file",
    "tools.code_action",
    "tools.generate",
    "tools.repo_analyze",
    "tools.soroban_skeleton",
    "workspaces.api_project_analyze",
    "workspaces.api_project_chat",
    "workspaces.api_project_chat_stream",
}


def _exhaust_user_budget(user, *, limit=100):
    budgets.upsert_budget(scope="user", scope_id=user.id, token_limit=limit, warn_threshold=0.8)
    db.session.add(
        UsageRecord(
            user_id=user.id,
            source="chat",
            total_tokens=limit,
            cost_estimate=0.0,
            status="success",
            created_at=datetime.now(UTC),
        )
    )
    db.session.commit()


def _exhaust_workspace_budget(user, workspace, *, limit=100):
    budgets.upsert_budget(
        scope="workspace", scope_id=workspace.id, token_limit=limit, warn_threshold=0.8
    )
    db.session.add(
        UsageRecord(
            user_id=user.id,
            workspace_id=workspace.id,
            source="project_chat",
            total_tokens=limit,
            cost_estimate=0.0,
            status="success",
            created_at=datetime.now(UTC),
        )
    )
    db.session.commit()


def _project(user, *, name="Billable project"):
    workspace = Workspace(user_id=user.id, name=f"{name} workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name=name,
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    db.session.add(
        ProjectFile(
            project_id=project.id,
            path="app/main.py",
            size=20,
            is_binary=False,
            content="def run():\n    pass\n",
        )
    )
    db.session.commit()
    return project


def _conversation(user):
    conversation = Conversation(user_id=user.id, title="Billable chat")
    db.session.add(conversation)
    db.session.commit()
    return conversation


def _assert_blocked(response):
    assert response.status_code == 429, response.get_json()
    body = response.get_json()
    assert body["kind"] == "budget_exceeded"
    assert body["limits_hit"] == ["tokens"]


class TestEnforcementCoverage:
    def test_every_billable_endpoint_is_guarded(self, app):
        """Fail when a billable route forgets the shared enforcement decorator."""
        guarded = {
            name
            for name, view in app.view_functions.items()
            if getattr(view, budgets.BUDGET_ENFORCED_ATTR, False)
        }
        assert BILLABLE_ENDPOINTS.issubset(guarded)
        assert guarded == BILLABLE_ENDPOINTS

    def test_guard_marks_the_view_without_changing_its_name(self):
        def view():
            return "ok"

        guarded = budgets.budget_guard(view)
        assert getattr(guarded, budgets.BUDGET_ENFORCED_ATTR) is True
        assert guarded.__name__ == "view"


class TestBlockedPaths:
    def test_chat_api_is_blocked_before_the_provider(self, client, make_user, login):
        user = make_user()
        conversation = _conversation(user)
        _exhaust_user_budget(user)
        login()
        before = UsageRecord.query.count()

        _assert_blocked(
            client.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"content": "hello"},
            )
        )
        assert db.session.get(Conversation, conversation.id).messages == []
        assert UsageRecord.query.count() == before

    def test_tools_generate_is_blocked(self, client, make_user, login):
        user = make_user()
        _exhaust_user_budget(user)
        login()
        before = UsageRecord.query.count()

        _assert_blocked(client.post("/tools/generate", json={"description": "a flask app"}))
        assert UsageRecord.query.count() == before

    def test_tools_code_action_is_blocked(self, client, make_user, login):
        user = make_user()
        _exhaust_user_budget(user)
        login()

        _assert_blocked(client.post("/tools/code", json={"action": "explain", "code": "x = 1"}))

    def test_reviews_are_blocked(self, client, make_user, login):
        user = make_user()
        _exhaust_user_budget(user)
        project = _project(user)
        login()

        _assert_blocked(
            client.post(
                "/reviews/api/reviews",
                json={"source": "project", "project_id": project.id, "kind": "quality"},
            )
        )

    def test_project_chat_is_blocked(self, client, make_user, login):
        user = make_user()
        _exhaust_user_budget(user)
        project = _project(user)
        login()

        _assert_blocked(
            client.post(
                f"/workspaces/api/projects/{project.id}/chat",
                json={"content": "what does this project do?"},
            )
        )

    def test_project_analysis_is_blocked(self, client, make_user, login):
        user = make_user()
        _exhaust_user_budget(user)
        project = _project(user)
        login()

        _assert_blocked(
            client.post(
                f"/workspaces/api/projects/{project.id}/analyze",
                json={"kind": "architecture"},
            )
        )

    def test_workspace_budget_blocks_project_chat(self, client, make_user, login):
        """A workspace budget applies to project routes via ``project_id``."""
        user = make_user()
        project = _project(user)
        _exhaust_workspace_budget(user, db.session.get(Workspace, project.workspace_id))
        login()

        response = client.post(
            f"/workspaces/api/projects/{project.id}/chat",
            json={"content": "hello"},
        )
        assert response.status_code == 429
        body = response.get_json()
        assert body["kind"] == "budget_exceeded"
        assert body["budgets"][0]["scope"] == "workspace"

    def test_github_analysis_is_blocked_before_github_is_called(self, client, make_user, login):
        user = make_user()
        _exhaust_user_budget(user)
        login()

        _assert_blocked(client.post("/github/api/repos/owner/repo/analyze"))
        _assert_blocked(client.post("/github/api/repos/owner/repo/analyze-file"))


class TestServiceLevelFallback:
    def test_analysis_service_fails_closed_without_calling_the_provider(
        self, app, make_user, monkeypatch
    ):
        user = make_user()
        _exhaust_user_budget(user)

        def _provider_must_not_be_built():
            raise AssertionError("provider must not be called when the budget is spent")

        monkeypatch.setattr(analysis, "get_provider", _provider_must_not_be_built)

        with app.test_request_context("/"):
            login_user(user)
            result = analysis.analyze_file("app.py", "python", "print('hi')")

        assert result["analysis"].startswith("[analysis unavailable: Budget exceeded")

    def test_budget_refusal_returns_none_when_a_call_is_allowed(self, app, make_user):
        user = make_user()
        budgets.upsert_budget(scope="user", scope_id=user.id, token_limit=100)

        with app.test_request_context("/"):
            login_user(user)
            assert budgets.budget_refusal(user) is None
