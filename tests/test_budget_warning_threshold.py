"""Configurable budget warning thresholds (issue #464).

The threshold is stored as a fraction of a limit (``0.8`` == 80%) and is
configured per budget. These tests pin the boundary behaviour: usage *below*
the threshold stays quiet, usage *at* the threshold warns, and a warning never
blocks the call.
"""

from datetime import UTC, datetime

import pytest

from app.extensions import db
from app.models import Conversation, UsageRecord
from app.services import budgets
from app.services.budget_alerts import ALERT_WARNING, check_budget_alerts


def _usage(user, *, tokens=0, cost=0.0, status="success"):
    record = UsageRecord(
        user_id=user.id,
        source="chat",
        total_tokens=tokens,
        cost_estimate=cost,
        status=status,
        created_at=datetime.now(UTC),
    )
    db.session.add(record)
    db.session.commit()
    return record


def _conversation(user):
    conversation = Conversation(user_id=user.id, title="Threshold")
    db.session.add(conversation)
    db.session.commit()
    return conversation


class TestTokenThresholdBoundary:
    def test_below_threshold_does_not_warn(self, app, make_user):
        user = make_user()
        budgets.upsert_budget(scope="user", scope_id=user.id, token_limit=1000, warn_threshold=0.8)
        _usage(user, tokens=799)

        status = budgets.evaluate_budget(budgets.budgets_for(user)[0])
        assert status.allowed
        assert not status.warned

    def test_exactly_at_threshold_warns_without_blocking(self, app, make_user):
        user = make_user()
        budgets.upsert_budget(scope="user", scope_id=user.id, token_limit=1000, warn_threshold=0.8)
        _usage(user, tokens=800)

        status = budgets.evaluate_budget(budgets.budgets_for(user)[0])
        assert status.token_ratio == pytest.approx(0.8)
        assert status.allowed, "a warning must never block the call"
        assert status.warned
        assert status.limits_hit == ()

    def test_custom_threshold_is_honoured_per_budget(self, app, make_user):
        early = make_user(username="early", email="early@example.com")
        late = make_user(username="late", email="late@example.com")
        budgets.upsert_budget(scope="user", scope_id=early.id, token_limit=1000, warn_threshold=0.5)
        budgets.upsert_budget(scope="user", scope_id=late.id, token_limit=1000, warn_threshold=0.9)
        _usage(early, tokens=500)
        _usage(late, tokens=500)

        early_status = budgets.evaluate_budget(budgets.budgets_for(early)[0])
        late_status = budgets.evaluate_budget(budgets.budgets_for(late)[0])
        assert early_status.warned
        assert not late_status.warned


class TestCostThresholdBoundary:
    def test_below_threshold_does_not_warn(self, app, make_user):
        user = make_user()
        budgets.upsert_budget(scope="user", scope_id=user.id, cost_limit=1.0, warn_threshold=0.8)
        _usage(user, cost=0.79)

        status = budgets.evaluate_budget(budgets.budgets_for(user)[0])
        assert status.allowed
        assert not status.warned

    def test_exactly_at_threshold_warns_without_blocking(self, app, make_user):
        user = make_user()
        budgets.upsert_budget(scope="user", scope_id=user.id, cost_limit=1.0, warn_threshold=0.8)
        _usage(user, cost=0.8)

        status = budgets.evaluate_budget(budgets.budgets_for(user)[0])
        assert status.cost_ratio == pytest.approx(0.8)
        assert status.allowed
        assert status.warned
        assert status.limits_hit == ()


class TestDefaultAndSerialization:
    def test_default_threshold_comes_from_config(self, app, make_user):
        app.config["BUDGET_DEFAULT_WARN_THRESHOLD"] = 0.25
        user = make_user()
        budget = budgets.upsert_budget(scope="user", scope_id=user.id, token_limit=100)
        assert budget.warn_threshold == pytest.approx(0.25)

        _usage(user, tokens=25)
        assert budgets.evaluate_budget(budget).warned

    def test_threshold_is_serialized_as_a_percentage(self, app, make_user):
        user = make_user()
        budget = budgets.upsert_budget(
            scope="user", scope_id=user.id, token_limit=100, warn_threshold=0.75
        )
        payload = budget.to_dict()
        assert payload["warn_threshold"] == pytest.approx(0.75)
        assert payload["warn_threshold_pct"] == pytest.approx(75.0)

    def test_api_returns_the_configured_percentage(self, client, make_user, login):
        make_user()
        login()
        response = client.put(
            "/billing/api/budgets",
            json={"scope": "user", "token_limit": 100, "warn_threshold": 0.6},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()["budget"]
        assert body["warn_threshold"] == pytest.approx(0.6)
        assert body["warn_threshold_pct"] == pytest.approx(60.0)

    @pytest.mark.parametrize("threshold", [0, -0.1, 1.01, 2])
    def test_invalid_thresholds_are_rejected(self, threshold):
        with pytest.raises(budgets.BudgetValidationError):
            budgets.validate_threshold(threshold)

    def test_full_threshold_is_valid(self):
        assert budgets.validate_threshold(1.0) == pytest.approx(1.0)


class TestWarningIsNonBlocking:
    def test_boundary_warning_still_serves_the_request(self, client, make_user, login):
        user = make_user()
        conversation = _conversation(user)
        budgets.upsert_budget(scope="user", scope_id=user.id, token_limit=100, warn_threshold=0.5)
        _usage(user, tokens=50)
        login()

        response = client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"content": "hello"},
        )

        assert response.status_code == 201, response.get_json()
        assert response.headers.get("X-Budget-Warning") == "approaching_limit"
        assert len(conversation.messages) == 2


class TestAlertServiceBoundary:
    def test_configured_ratio_controls_the_warning_alert(self):
        from app.services import budget_alerts

        budget_alerts.reset()
        assert check_budget_alerts(1, spend=89, limit=100, period="2026-10", warn_ratio=0.9) == []

        alerts = check_budget_alerts(1, spend=90, limit=100, period="2026-10", warn_ratio=0.9)
        assert [alert["kind"] for alert in alerts] == [ALERT_WARNING]
