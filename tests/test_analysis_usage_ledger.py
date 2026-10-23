"""GitHub analysis must record one usage-ledger row per LLM call (#366)."""

from __future__ import annotations

from types import SimpleNamespace

from flask_login import login_user

from app.models import UsageRecord
from app.services import analysis, budgets


class FakeProvider:
    name = "mock"
    model = "mock-1"
    models = ("mock-1",)

    def complete(self, messages, *, stream=False):
        return "analysis result"

    def chat(self, messages, **kwargs):
        raise AssertionError("analysis must keep using the provider's complete() path")


def test_github_analysis_records_a_ledger_row(app, db, make_user, monkeypatch):
    user = make_user(username="analyst", email="analyst@example.com")
    monkeypatch.setattr(analysis, "get_provider", lambda: FakeProvider())
    monkeypatch.setattr(
        budgets,
        "check_budgets",
        lambda current_user: SimpleNamespace(allowed=True, message=""),
    )
    monkeypatch.setattr(budgets, "after_usage_recorded", lambda record: None)

    with app.test_request_context("/"):
        login_user(user)
        assert analysis._run("review this change") == "analysis result"

    row = UsageRecord.query.order_by(UsageRecord.id.desc()).first()
    assert row is not None
    assert row.source == "analysis"
    assert row.user_id == user.id
    assert row.provider == "mock"
    assert row.model == "mock-1"
    assert row.total_tokens == 0
    assert row.tokens_estimated is True
