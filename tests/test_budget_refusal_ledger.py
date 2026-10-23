"""Refused billed calls are recorded with a denied ledger status (#463)."""

from app.models import UsageRecord
from app.services import budgets


def test_refused_call_records_denied_ledger_row(app, db, make_user, monkeypatch):
    user = make_user(username="budgetowner", email="budgetowner@example.com")
    decision = budgets.BudgetDecision(
        allowed=False,
        warned=False,
        limits_hit=("tokens",),
        message="Token limit reached.",
    )
    monkeypatch.setattr(budgets, "check_budgets", lambda user, workspace_id=None: decision)
    monkeypatch.setattr(budgets, "after_usage_recorded", lambda record: None)

    with app.test_request_context("/chat/api/chat", method="POST"):
        response = budgets.budget_refusal(user)

    assert response.status_code == 429
    row = UsageRecord.query.order_by(UsageRecord.id.desc()).first()
    assert row is not None
    assert row.user_id == user.id
    assert row.status == "denied"
    assert row.total_tokens == 0
    assert row.tokens_estimated is True
