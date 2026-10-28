"""Admin usage report page tests (#480).

The page is admin-only, filters by date window and scope (user/workspace), and
renders a bounded, paginated table from stored usage rows.
"""

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import UsageRecord, User, Workspace

NOW = datetime.now(UTC)


def _user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _admin(username="admin", email="admin@example.com"):
    user = _user(username, email)
    user.is_admin = True
    db.session.commit()
    return user


def _workspace(owner, name="Report workspace"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _record(
    user,
    *,
    days_ago=0,
    workspace=None,
    source="chat",
    provider="openai",
    model="gpt-4o",
    total_tokens=15,
):
    record = UsageRecord(
        user_id=user.id,
        workspace_id=workspace.id if workspace else None,
        source=source,
        provider=provider,
        model=model,
        prompt_tokens=total_tokens - 5,
        completion_tokens=5,
        total_tokens=total_tokens,
        tokens_estimated=False,
        cost_estimate=0.01,
        status="success",
        created_at=NOW - timedelta(days=days_ago),
    )
    db.session.add(record)
    db.session.commit()
    return record


class TestAdminUsageReportAccess:
    def test_admin_can_open_the_report(self, client, login):
        _admin()
        login(email="admin@example.com")
        response = client.get("/admin/usage/report")
        assert response.status_code == 200
        assert b"Admin usage report" in response.data
        assert b"No usage rows match this window and scope." in response.data

    def test_non_admin_is_forbidden(self, client, login):
        _user("owner", "owner@example.com")
        login(email="owner@example.com")
        assert client.get("/admin/usage/report").status_code == 403

    def test_unauthenticated_is_redirected_to_login(self, client):
        assert client.get("/admin/usage/report").status_code == 302

    def test_admin_navigation_link_is_visible_only_to_admins(self, client, login):
        _admin()
        _user("owner", "owner@example.com")

        login(email="admin@example.com")
        assert b"Admin usage" in client.get("/").data

        login(email="owner@example.com")
        assert b"Admin usage" not in client.get("/").data


class TestAdminUsageReportFilters:
    def test_user_scope_filters_rows_and_totals(self, client, login):
        first = _user("first", "first@example.com")
        second = _user("second", "second@example.com")
        _admin()
        _record(first, provider="openai", total_tokens=15)
        _record(second, provider="anthropic", total_tokens=30)
        login(email="admin@example.com")

        response = client.get(f"/admin/usage/report?days=7&scope=user&user_id={first.id}")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "<td>first</td>" in html
        assert "<td>anthropic</td>" not in html
        assert "<td>openai</td>" in html

    def test_workspace_scope_filters_rows(self, client, login):
        owner = _user("owner", "owner@example.com")
        other = _user("other", "other@example.com")
        workspace = _workspace(owner)
        _admin()
        _record(owner, workspace=workspace, provider="openai")
        _record(other, provider="anthropic")
        login(email="admin@example.com")

        response = client.get(
            f"/admin/usage/report?days=7&scope=workspace&workspace_id={workspace.id}"
        )

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "<td>Report workspace</td>" in html
        assert "<td>anthropic</td>" not in html
        assert "<td>openai</td>" in html

    def test_date_window_excludes_older_rows(self, client, login):
        user = _user("owner", "owner@example.com")
        _admin()
        _record(user, days_ago=0, provider="openai")
        _record(user, days_ago=30, provider="anthropic")
        login(email="admin@example.com")

        response = client.get("/admin/usage/report?days=7")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "<td>openai</td>" in html
        assert "<td>anthropic</td>" not in html

    def test_source_filter_narrows_the_table(self, client, login):
        user = _user("owner", "owner@example.com")
        _admin()
        _record(user, source="chat", provider="openai")
        _record(user, source="analysis", provider="anthropic")
        login(email="admin@example.com")

        response = client.get("/admin/usage/report?days=7&source=analysis")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "<td>analysis</td>" in html
        assert "<td>chat</td>" not in html
        assert "<td>anthropic</td>" in html
        assert "<td>openai</td>" not in html

    def test_unknown_scope_is_rejected(self, client, login):
        _admin()
        login(email="admin@example.com")
        response = client.get("/admin/usage/report?scope=team")
        assert response.status_code == 400

    def test_missing_scope_id_yields_an_empty_report(self, client, login):
        _admin()
        _user("owner", "owner@example.com")
        login(email="admin@example.com")
        response = client.get("/admin/usage/report?scope=user")
        assert response.status_code == 200
        assert b"No usage rows match this window and scope." in response.data


class TestAdminUsageReportTableBounds:
    def test_per_page_is_capped_and_pagination_works(self, client, login):
        user = _user("owner", "owner@example.com")
        _admin()
        for _ in range(3):
            _record(user)
        login(email="admin@example.com")

        response = client.get("/admin/usage/report?days=7&per_page=1000")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'name="per_page" type="number" min="1" max="100" value="100"' in html
        assert "Page 1 of 1" in html

        second = client.get("/admin/usage/report?days=7&per_page=2&page=2")
        assert second.status_code == 200
        assert b"Page 2 of 2" in second.data

    def test_filters_persist_in_pagination_links(self, client, login):
        user = _user("owner", "owner@example.com")
        _admin()
        for _ in range(3):
            _record(user, source="chat")
        login(email="admin@example.com")

        response = client.get(
            f"/admin/usage/report?days=7&scope=user&user_id={user.id}&source=chat&per_page=2"
        )

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "source=chat" in html
        assert f"user_id={user.id}" in html

    def test_cost_values_are_explicitly_labelled_as_estimates(self, client, login):
        user = _user("cost-reader", "cost-reader@example.com")
        _admin()
        _record(user)
        login(email="admin@example.com")

        response = client.get("/admin/usage/report")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'title="Estimated from the configured price table; not a provider-confirmed charge."' in html
        assert 'aria-label="Estimated cost, USD"' in html
        assert "<th scope=\"col\" title=\"Estimated from the configured price table; not a provider-confirmed charge.\">Estimated cost</th>" in html
