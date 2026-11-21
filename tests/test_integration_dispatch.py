"""Tests for event fan-out to configured integrations."""

from __future__ import annotations

from app.models import Workspace
from app.services import budget_alerts
from app.services.events import emit_event
from app.services.integrations.dispatch import dispatch_workspace_event

WEBHOOK = "https://hooks.slack.com/services/T/B/secret"


class _Response:
    status_code = 200


def _workspace(db, owner):
    row = Workspace(user_id=owner.id, name="Dispatch Workspace")
    db.session.add(row)
    db.session.commit()
    return row


def _configure_slack(monkeypatch, row, owner):
    from app.services.integrations import slack

    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value)
    slack.configure(row.id, owner, WEBHOOK)


def test_dispatch_skips_unconfigured_workspace(db, make_user):
    owner = make_user()
    row = _workspace(db, owner)
    assert dispatch_workspace_event(row.id, "review.completed", {}) == []


def test_review_event_reaches_configured_slack(monkeypatch, db, make_user):
    owner = make_user()
    row = _workspace(db, owner)
    _configure_slack(monkeypatch, row, owner)
    calls = []
    monkeypatch.setattr(
        "app.services.integrations.slack.requests.post",
        lambda url, **kwargs: calls.append(kwargs) or _Response(),
    )

    result = dispatch_workspace_event(
        row.id,
        "review.completed",
        {"review_id": 3, "kind": "security", "status": "completed"},
    )

    assert [item["integration"] for item in result] == ["slack"]
    assert result[0]["status"] == "sent"
    assert len(calls) == 1


def test_registered_handler_reacts_to_review_event(monkeypatch, db, make_user):
    owner = make_user()
    row = _workspace(db, owner)
    _configure_slack(monkeypatch, row, owner)
    calls = []
    monkeypatch.setattr(
        "app.services.integrations.slack.requests.post",
        lambda url, **kwargs: calls.append(kwargs) or _Response(),
    )

    emit_event(
        "review.completed",
        data={"review_id": 4, "kind": "quality", "status": "completed"},
        workspace_id=row.id,
        user_id=owner.id,
    )

    assert len(calls) == 1


def test_budget_alerts_fan_out_to_configured_slack(monkeypatch, db, make_user):
    owner = make_user()
    row = _workspace(db, owner)
    _configure_slack(monkeypatch, row, owner)
    calls = []
    monkeypatch.setattr(
        "app.services.integrations.slack.requests.post",
        lambda url, **kwargs: calls.append(kwargs) or _Response(),
    )

    budget_alerts.notify_budget_alerts(
        [
            {
                "workspace_id": row.id,
                "kind": "over_budget",
                "period": "2026-01",
                "spend": 110,
                "limit": 100,
            }
        ],
        [owner],
        workspace=row,
    )

    assert len(calls) == 1
    assert "Budget alert" in calls[0]["json"]["text"]
