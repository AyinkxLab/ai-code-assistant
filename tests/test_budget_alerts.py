"""Budget threshold alerts (#371): 80% warning + 100% over-budget, dedupe, prefs."""

from app.services import budget_alerts
from app.services.budget_alerts import (
    ALERT_OVER_BUDGET,
    ALERT_WARNING,
    check_budget_alerts,
)


def kinds(alerts):
    return [alert["kind"] for alert in alerts]


def test_no_alert_below_warning_threshold():
    budget_alerts.reset()
    assert check_budget_alerts(1, spend=79.9, limit=100, period="2026-01") == []


def test_warning_fires_at_the_eighty_percent_boundary():
    budget_alerts.reset()
    alerts = check_budget_alerts(1, spend=80, limit=100, period="2026-01")
    assert kinds(alerts) == [ALERT_WARNING]
    assert alerts[0]["ratio"] == 0.8


def test_warning_fires_only_once_per_period():
    budget_alerts.reset()
    assert kinds(check_budget_alerts(1, spend=80, limit=100, period="2026-01")) == [ALERT_WARNING]
    # Further crossings in the same period are suppressed.
    assert check_budget_alerts(1, spend=85, limit=100, period="2026-01") == []
    assert check_budget_alerts(1, spend=99.9, limit=100, period="2026-01") == []


def test_over_budget_fires_once_after_warning():
    budget_alerts.reset()
    check_budget_alerts(1, spend=80, limit=100, period="2026-01")
    alerts = check_budget_alerts(1, spend=100, limit=100, period="2026-01")
    assert kinds(alerts) == [ALERT_OVER_BUDGET]
    assert alerts[0]["over_by"] == 0.0
    # Repeated over-budget crossings stay suppressed.
    assert check_budget_alerts(1, spend=150, limit=100, period="2026-01") == []


def test_jumping_straight_over_budget_emits_both_thresholds_once():
    budget_alerts.reset()
    alerts = check_budget_alerts(1, spend=110, limit=100, period="2026-01")
    assert kinds(alerts) == [ALERT_WARNING, ALERT_OVER_BUDGET]
    assert alerts[1]["over_by"] == 10.0
    assert check_budget_alerts(1, spend=120, limit=100, period="2026-01") == []


def test_a_new_period_resets_deduplication():
    budget_alerts.reset()
    assert kinds(check_budget_alerts(1, spend=80, limit=100, period="2026-01")) == [ALERT_WARNING]
    assert kinds(check_budget_alerts(1, spend=80, limit=100, period="2026-02")) == [ALERT_WARNING]


def test_deduplication_is_scoped_per_workspace():
    budget_alerts.reset()
    assert kinds(check_budget_alerts(1, spend=80, limit=100, period="2026-01")) == [ALERT_WARNING]
    assert kinds(check_budget_alerts(2, spend=80, limit=100, period="2026-01")) == [ALERT_WARNING]


def test_no_alert_without_a_positive_limit():
    budget_alerts.reset()
    assert check_budget_alerts(1, spend=50, limit=0, period="2026-01") == []
    assert check_budget_alerts(1, spend=50, limit=-1, period="2026-01") == []
    assert check_budget_alerts(1, spend=50, limit=None, period="2026-01") == []


def test_invalid_inputs_do_not_raise():
    budget_alerts.reset()
    assert check_budget_alerts(1, spend="nope", limit=100, period="2026-01") == []
    assert check_budget_alerts(1, spend=50, limit=100, period="2026-01", warn_ratio="bad") == []


def test_custom_warning_ratio_is_honoured():
    budget_alerts.reset()
    assert kinds(check_budget_alerts(1, spend=50, limit=100, period="2026-01", warn_ratio=0.5)) == [
        ALERT_WARNING
    ]


def test_notify_budget_alerts_honours_preferences(app, db, make_user):
    from app.models.notification import Notification
    from app.models.notification_preference import NotificationPreference

    allowed = make_user(username="allowed", email="allowed@example.com")
    opted_out = make_user(username="optout", email="optout@example.com")
    db.session.add(NotificationPreference(user_id=allowed.id, budget_alerts=True))
    db.session.add(NotificationPreference(user_id=opted_out.id, budget_alerts=False))
    db.session.commit()

    alerts = [
        {
            "workspace_id": 1,
            "kind": ALERT_OVER_BUDGET,
            "period": "2026-01",
            "spend": 110.0,
            "limit": 100.0,
            "ratio": 1.1,
            "over_by": 10.0,
        }
    ]
    created = budget_alerts.notify_budget_alerts(alerts, [allowed, opted_out])

    assert [notification.user_id for notification in created] == [allowed.id]
    assert Notification.query.filter_by(user_id=opted_out.id).count() == 0
