"""Budget threshold notifications honour preferences (Phase 10, #470)."""

from __future__ import annotations

from app.extensions import db
from app.models import Notification, NotificationPreference, Workspace
from app.services import budgets, usage_ledger


def _token_budget(user, limit, *, threshold=0.5):
    return budgets.upsert_budget(
        scope="user", scope_id=user.id, token_limit=limit, warn_threshold=threshold
    )


def _levels(user):
    rows = Notification.query.filter_by(user_id=user.id, type="budget_alert").all()
    return sorted(row.payload["level"] for row in rows)


def test_notifications_fire_at_warning_and_limit_thresholds(app, make_user):
    user = make_user()
    _token_budget(user, 10)

    usage_ledger.record_usage(source="chat", user=user, total_tokens=5)
    assert _levels(user) == ["warning"]

    usage_ledger.record_usage(source="chat", user=user, total_tokens=5)
    assert _levels(user) == ["blocked", "warning"]


def test_opted_out_user_receives_neither_threshold_notification(app, make_user):
    user = make_user()
    db.session.add(NotificationPreference(user_id=user.id, budget_alerts=False))
    db.session.commit()
    _token_budget(user, 10)

    usage_ledger.record_usage(source="chat", user=user, total_tokens=5)
    usage_ledger.record_usage(source="chat", user=user, total_tokens=5)

    assert _levels(user) == []
    assert Notification.query.filter_by(type="budget_alert").count() == 0


def test_workspace_budget_notifies_owner_and_respects_owner_preference(app, make_user):
    owner = make_user(email="owner@example.com", username="owner")
    member = make_user(email="member@example.com", username="member")
    workspace = Workspace(user_id=owner.id, name="Team")
    db.session.add(workspace)
    db.session.add(NotificationPreference(user_id=owner.id, budget_alerts=False))
    db.session.commit()
    budgets.upsert_budget(
        scope="workspace",
        scope_id=workspace.id,
        token_limit=10,
        warn_threshold=0.5,
    )

    usage_ledger.record_usage(source="chat", user=member, workspace=workspace, total_tokens=6)

    assert _levels(owner) == []  # owner opted out
    assert _levels(member) == ["warning"]


def test_workspace_budget_notifies_owner_by_default(app, make_user):
    owner = make_user(email="owner2@example.com", username="owner2")
    member = make_user(email="member2@example.com", username="member2")
    workspace = Workspace(user_id=owner.id, name="Team")
    db.session.add(workspace)
    db.session.commit()
    budgets.upsert_budget(
        scope="workspace", scope_id=workspace.id, token_limit=10, warn_threshold=0.5
    )

    usage_ledger.record_usage(source="chat", user=member, workspace=workspace, total_tokens=10)

    # Jumping straight past the limit reports the more severe level once.
    assert _levels(owner) == ["blocked"]
    assert _levels(member) == ["blocked"]
