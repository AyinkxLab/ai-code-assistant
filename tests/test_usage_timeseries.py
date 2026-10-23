"""Usage time-series aggregation tests (#449).

Buckets are computed strictly from stored usage rows, include empty calendar
periods, and are bounded by the selected window. Daily buckets cover UTC dates;
weekly buckets start on Monday and end on Sunday.
"""

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import UsageRecord, User, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_VIEWER, STATUS_ACTIVE
from app.services import usage_dashboards

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _record(
    user,
    *,
    days_ago=0,
    created_at=None,
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
        created_at=created_at or (NOW - timedelta(days=days_ago)),
    )
    db.session.add(record)
    db.session.commit()
    return record


def _by_start(payload):
    return {row["start"]: row for row in payload["buckets"]}


class TestTimeseriesBuckets:
    def test_daily_buckets_include_empty_days_and_stored_totals(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")

        _record(user, days_ago=0)
        _record(user, days_ago=2, prompt_tokens=20, completion_tokens=10, cost_estimate=0.02)
        _record(user, days_ago=20)

        payload = usage_dashboards.timeseries(user_id=user.id, days=7, now=NOW)

        assert payload["bucket"] == "day"
        assert payload["totals"]["count"] == 2
        assert payload["totals"]["total_tokens"] == 45
        assert payload["totals"]["cost_estimate"] == 0.03

        starts = [row["start"] for row in payload["buckets"]]
        assert starts == [
            "2026-10-13",
            "2026-10-14",
            "2026-10-15",
            "2026-10-16",
            "2026-10-17",
            "2026-10-18",
            "2026-10-19",
            "2026-10-20",
        ]
        buckets = _by_start(payload)
        assert buckets["2026-10-18"]["totals"]["total_tokens"] == 30
        assert buckets["2026-10-20"]["totals"]["total_tokens"] == 15
        assert buckets["2026-10-19"]["totals"]["count"] == 0
        assert buckets["2026-10-20"]["end"] == "2026-10-20"

    def test_weekly_buckets_start_on_monday_and_end_on_sunday(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, days_ago=2)  # Sunday, 2026-10-18
        _record(user, days_ago=1)  # Monday, 2026-10-19

        payload = usage_dashboards.timeseries(user_id=user.id, days=7, bucket="week", now=NOW)

        assert payload["bucket"] == "week"
        starts = [row["start"] for row in payload["buckets"]]
        assert starts == ["2026-10-12", "2026-10-19"]
        buckets = _by_start(payload)
        assert buckets["2026-10-12"]["end"] == "2026-10-18"
        assert buckets["2026-10-19"]["end"] == "2026-10-25"
        assert buckets["2026-10-12"]["totals"]["count"] == 1
        assert buckets["2026-10-19"]["totals"]["count"] == 1

    def test_empty_scope_returns_zeroed_buckets(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        payload = usage_dashboards.timeseries(user_id=user.id, days=3, now=NOW)

        assert payload["totals"]["count"] == 0
        assert payload["buckets"]
        assert all(row["totals"]["total_tokens"] == 0 for row in payload["buckets"])

    def test_range_is_bounded_and_excludes_future_rows(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, days_ago=20)
        _record(user, created_at=NOW + timedelta(days=1))

        payload = usage_dashboards.timeseries(user_id=user.id, days=7, now=NOW)

        assert payload["totals"]["count"] == 0
        assert len(payload["buckets"]) == 8
        assert payload["range"]["days"] == 7

    def test_partial_first_bucket_only_contains_rows_inside_the_window(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, created_at=NOW - timedelta(days=7))
        _record(user, created_at=NOW - timedelta(days=8))
        _record(user, days_ago=0)

        payload = usage_dashboards.timeseries(user_id=user.id, days=7, now=NOW)

        buckets = _by_start(payload)
        assert buckets["2026-10-13"]["totals"]["count"] == 1
        assert payload["totals"]["count"] == 2

    def test_filters_apply_before_aggregation(self, db, make_user):
        user = make_user(username="owner", email="owner@example.com")
        _record(user, days_ago=0, source="chat", provider="openai", model="gpt-4o")
        _record(
            user,
            days_ago=1,
            source="analysis",
            provider="anthropic",
            model="claude",
            prompt_tokens=20,
            completion_tokens=10,
        )

        payload = usage_dashboards.timeseries(user_id=user.id, days=7, now=NOW, source="analysis")

        assert payload["totals"]["count"] == 1
        assert payload["filters"]["source"] == "analysis"
        buckets = _by_start(payload)
        assert buckets["2026-10-19"]["totals"]["total_tokens"] == 30
        assert buckets["2026-10-20"]["totals"]["count"] == 0


class TestTimeseriesApi:
    def test_owner_sees_only_own_buckets(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="other", email="other@example.com")
        _record(owner, days_ago=0)
        login(email="owner@example.com")

        response = client.get("/billing/api/usage/timeseries?days=7&bucket=day")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["totals"]["count"] == 1
        assert payload["bucket"] == "day"

        login(email="other@example.com")
        response = client.get("/billing/api/usage/timeseries?days=7")
        assert response.status_code == 200
        assert response.get_json()["totals"]["count"] == 0

    def test_invalid_window_and_bucket_are_rejected(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")

        for value in ("0", "366", "junk"):
            response = client.get(f"/billing/api/usage/timeseries?days={value}")
            assert response.status_code == 400

        response = client.get("/billing/api/usage/timeseries?bucket=month")
        assert response.status_code == 400
        assert response.get_json()["kind"] == "invalid"

    def test_workspace_timeseries_requires_owner_or_admin(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        admin = make_user(username="admin", email="admin@example.com")
        admin.is_admin = True
        db.session.commit()
        workspace = Workspace(user_id=owner.id, name="Analytics")
        db.session.add(workspace)
        db.session.commit()
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=member.id,
                role=ROLE_VIEWER,
                status=STATUS_ACTIVE,
            )
        )
        db.session.commit()
        _record(owner, workspace=workspace)

        login(email="owner@example.com")
        response = client.get(f"/billing/api/workspaces/{workspace.id}/usage/timeseries")
        assert response.status_code == 200
        assert response.get_json()["totals"]["count"] == 1

        login(email="member@example.com")
        response = client.get(f"/billing/api/workspaces/{workspace.id}/usage/timeseries")
        assert response.status_code == 404

        login(email="admin@example.com")
        response = client.get(f"/billing/api/workspaces/{workspace.id}/usage/timeseries")
        assert response.status_code == 200
