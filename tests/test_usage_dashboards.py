"""Usage and cost dashboard tests (#368).

Aggregation is computed strictly from stored usage rows, scoped per user or
workspace, and authorization matches the budget rules: user data is owner-only;
workspace data is owner/admin-only. CSV exports are bounded.
"""

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import UsageRecord, User, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_VIEWER, STATUS_ACTIVE
from app.services import usage_dashboards

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _user(username, email, *, is_admin=False):
    user = User(username=username, email=email, is_admin=is_admin)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner, name="Dashboard workspace"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _member(workspace, user, role=ROLE_VIEWER):
    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=role,
        status=STATUS_ACTIVE,
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def _record(
    user,
    *,
    days_ago=0,
    workspace=None,
    source="chat",
    provider="openai",
    model="gpt-4o",
    prompt_tokens=10,
    completion_tokens=5,
    cost_estimate=0.01,
):
    record = UsageRecord(
        user_id=user.id,
        workspace_id=workspace.id if workspace else None,
        source=source,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        tokens_estimated=False,
        cost_estimate=cost_estimate,
        status="success",
        created_at=NOW - timedelta(days=days_ago),
    )
    db.session.add(record)
    db.session.commit()
    return record


class TestDashboardAggregation:
    def test_aggregates_totals_and_breakdowns_from_stored_rows(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        _record(user, days_ago=0, provider="openai", model="gpt-4o", cost_estimate=0.01)
        _record(
            user,
            days_ago=1,
            provider="anthropic",
            model="claude",
            prompt_tokens=20,
            completion_tokens=10,
            cost_estimate=0.02,
        )
        _record(other, days_ago=0, provider="openai", model="gpt-4o", cost_estimate=99)

        payload = usage_dashboards.dashboard(user_id=user.id, days=7, now=NOW)

        assert payload["scope"]["type"] == "user"
        assert payload["totals"]["count"] == 2
        assert payload["totals"]["prompt_tokens"] == 30
        assert payload["totals"]["completion_tokens"] == 15
        assert payload["totals"]["total_tokens"] == 45
        assert payload["totals"]["cost_estimate"] == 0.03
        assert payload["totals"]["priced_records"] == 2

        providers = {row["provider"]: row for row in payload["by_provider"]}
        assert set(providers) == {"openai", "anthropic"}
        assert providers["openai"]["total_tokens"] == 15
        assert providers["anthropic"]["total_tokens"] == 30

        models = {row["model"]: row for row in payload["by_model"]}
        assert set(models) == {"gpt-4o", "claude"}

        sources = {row["source"]: row for row in payload["by_source"]}
        assert sources["chat"]["total_tokens"] == 45

        assert [row["date"] for row in payload["daily"]] == [
            "2026-10-19",
            "2026-10-20",
        ]
        assert payload["daily"][1]["total_tokens"] == 15

    def test_empty_scope_has_a_stable_zero_shape(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        payload = usage_dashboards.dashboard(user_id=user.id, days=30, now=NOW)

        assert payload["totals"]["count"] == 0
        assert payload["totals"]["total_tokens"] == 0
        assert payload["by_provider"] == []
        assert payload["by_model"] == []
        assert payload["daily"] == []
        assert payload["by_source"]

    def test_window_excludes_older_rows(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, days_ago=10)
        payload = usage_dashboards.dashboard(user_id=user.id, days=7, now=NOW)
        assert payload["totals"]["count"] == 0

    def test_admin_dashboard_is_org_wide_with_by_user(self, db, make_user):
        first = make_user(username="first", email="first@example.com")
        second = make_user(username="second", email="second@example.com")
        _record(first, cost_estimate=0.01)
        _record(second, cost_estimate=0.02)

        payload = usage_dashboards.admin_dashboard(days=7, now=NOW)

        assert payload["scope"]["type"] == "admin"
        assert payload["totals"]["count"] == 2
        assert payload["totals"]["cost_estimate"] == 0.03
        users = {row["user_id"]: row for row in payload["by_user"]}
        assert set(users) == {str(first.id), str(second.id)}
        assert users[str(first.id)]["total_tokens"] == 15


class TestDashboardApi:
    def test_owner_sees_only_own_usage(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="other", email="other@example.com")
        _record(owner)
        login(email="owner@example.com")

        response = client.get("/billing/api/usage/dashboard?days=7")
        assert response.status_code == 200
        assert response.get_json()["totals"]["count"] == 1

        login(email="other@example.com")
        response = client.get("/billing/api/usage/dashboard?days=7")
        assert response.status_code == 200
        assert response.get_json()["totals"]["count"] == 0

    def test_invalid_window_is_rejected(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        for value in ("0", "366", "junk"):
            response = client.get(f"/billing/api/usage/dashboard?days={value}")
            assert response.status_code == 400

    def test_page_renders_totals_and_empty_state(self, client, make_user, login):
        user = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")

        empty = client.get("/billing/usage")
        assert empty.status_code == 200
        assert b"No usage in this window." in empty.data

        _record(user)
        populated = client.get("/billing/usage")
        assert populated.status_code == 200
        assert b"15" in populated.data
        assert b"gpt-4o" in populated.data

    def test_workspace_dashboard_requires_owner_or_admin(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        make_user(username="outsider", email="outsider@example.com")
        admin = make_user(username="admin", email="admin@example.com")
        admin.is_admin = True
        db.session.commit()
        workspace = _workspace(owner)
        _member(workspace, member)
        _record(owner, workspace=workspace)

        login(email="owner@example.com")
        response = client.get(f"/billing/api/workspaces/{workspace.id}/usage")
        assert response.status_code == 200
        assert response.get_json()["totals"]["count"] == 1

        for email in ("member@example.com", "outsider@example.com"):
            login(email=email)
            response = client.get(f"/billing/api/workspaces/{workspace.id}/usage")
            assert response.status_code == 404
            assert response.get_json()["kind"] == "not_found"

        login(email="admin@example.com")
        response = client.get(f"/billing/api/workspaces/{workspace.id}/usage")
        assert response.status_code == 200
        assert response.get_json()["totals"]["count"] == 1

    def test_workspace_page_requires_access(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        _member(workspace, member)
        login(email="member@example.com")
        assert client.get(f"/billing/usage?workspace_id={workspace.id}").status_code == 404


class TestCsvExport:
    def test_user_export_has_a_header_and_only_own_rows(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="other", email="other@example.com")
        _record(owner, provider="openai")
        login(email="owner@example.com")

        response = client.get("/billing/api/usage/dashboard.csv?days=7")
        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        assert "attachment" in response.headers["Content-Disposition"]
        lines = response.get_data(as_text=True).splitlines()
        assert lines[0].startswith("date,user_id,workspace_id,provider,model,source")
        assert len(lines) == 2
        assert "openai" in lines[1]

    def test_export_is_bounded_by_rows(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        for _ in range(3):
            _record(user)

        text, truncated = usage_dashboards.export_csv(user_id=user.id, days=7, now=NOW, max_rows=1)
        assert truncated is True
        assert len(text.splitlines()) == 2

    def test_workspace_export_requires_owner_or_admin(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        _member(workspace, member)

        login(email="member@example.com")
        response = client.get(f"/billing/api/workspaces/{workspace.id}/usage.csv")
        assert response.status_code == 404

        login(email="owner@example.com")
        response = client.get(f"/billing/api/workspaces/{workspace.id}/usage.csv")
        assert response.status_code == 200
        assert response.mimetype == "text/csv"


class TestAdminView:
    def test_admin_org_wide_view_and_export(self, client, make_user, login):
        first = make_user(username="first", email="first@example.com")
        second = make_user(username="second", email="second@example.com")
        admin = make_user(username="admin", email="admin@example.com")
        admin.is_admin = True
        db.session.commit()
        _record(first)
        _record(second)

        login(email="admin@example.com")
        response = client.get("/admin/usage?days=7")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["totals"]["count"] == 2
        assert len(payload["by_user"]) == 2

        export = client.get("/admin/usage/export.csv?days=7")
        assert export.status_code == 200
        assert export.mimetype == "text/csv"
        assert len(export.get_data(as_text=True).splitlines()) == 3

    def test_non_admin_is_forbidden(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        response = client.get("/admin/usage")
        assert response.status_code == 403

    def test_unauthenticated_is_redirected_to_login(self, client):
        assert client.get("/admin/usage").status_code == 302


class TestDashboardFilters:
    def test_filters_narrow_every_panel(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, source="chat", provider="openai", model="gpt-4o")
        _record(
            user,
            source="analysis",
            provider="anthropic",
            model="claude",
            prompt_tokens=20,
            completion_tokens=10,
            cost_estimate=0.02,
        )

        payload = usage_dashboards.dashboard(user_id=user.id, source="analysis", now=NOW)

        assert payload["totals"]["count"] == 1
        assert payload["totals"]["total_tokens"] == 30
        assert [row["provider"] for row in payload["by_provider"]] == ["anthropic"]
        assert [row["model"] for row in payload["by_model"]] == ["claude"]
        sources = {row["source"]: row for row in payload["by_source"]}
        assert sources["analysis"]["total_tokens"] == 30
        assert sources["chat"]["total_tokens"] == 0
        assert [row["date"] for row in payload["daily"]] == ["2026-10-20"]
        assert payload["filters"] == {"source": "analysis", "provider": None, "model": None}
        assert payload["scope"]["filters"]["source"] == "analysis"
        # Available values still describe the whole window so the UI can switch.
        assert payload["available_filters"]["sources"] == ["analysis", "chat"]

    def test_provider_and_model_filters_combine(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, provider="openai", model="gpt-4o")
        _record(
            user, provider="openai", model="gpt-4o-mini", prompt_tokens=20, completion_tokens=10
        )
        _record(user, provider="anthropic", model="claude", prompt_tokens=30, completion_tokens=15)

        payload = usage_dashboards.dashboard(
            user_id=user.id, provider="openai", model="gpt-4o-mini", now=NOW
        )

        assert payload["totals"]["count"] == 1
        assert payload["totals"]["total_tokens"] == 30
        assert [row["model"] for row in payload["by_model"]] == ["gpt-4o-mini"]

    def test_blank_filters_mean_no_filter(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, source="chat")
        _record(user, source="analysis")

        payload = usage_dashboards.dashboard(user_id=user.id, source="   ", now=NOW)

        assert payload["filters"]["source"] is None
        assert payload["totals"]["count"] == 2

    def test_export_applies_the_same_filters(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, source="chat", cost_estimate=0.01)
        _record(user, source="analysis", cost_estimate=0.02)

        text, truncated = usage_dashboards.export_csv(user_id=user.id, source="analysis", now=NOW)

        assert truncated is False
        lines = text.strip().splitlines()
        assert len(lines) == 2
        assert "analysis" in lines[1]
        assert "chat" not in lines[1]

    def test_page_persists_filters_in_the_url(self, client, make_user, login):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, source="chat", provider="openai", model="gpt-4o")
        login(email="owner@example.com")

        response = client.get("/billing/usage?days=30&source=chat&provider=openai&model=gpt-4o")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'value="chat" selected' in html
        assert 'value="openai" selected' in html
        assert 'value="gpt-4o" selected' in html
        assert "source=chat" in html

    def test_api_returns_the_selected_filters(self, client, make_user, login):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, source="chat")
        _record(user, source="analysis", prompt_tokens=20, completion_tokens=10)
        login(email="owner@example.com")

        response = client.get("/billing/api/usage/dashboard?source=analysis")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["totals"]["count"] == 1
        assert payload["filters"]["source"] == "analysis"
