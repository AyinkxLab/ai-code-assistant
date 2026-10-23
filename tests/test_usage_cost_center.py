"""Cost-center tagging and rollups (#484)."""

from app.services import usage_dashboards, usage_ledger


def test_usage_rolls_up_by_cost_center(app, db, make_user):
    user = make_user(username="costowner", email="costowner@example.com")

    usage_ledger.record_usage(source="chat", user=user, cost_center="engineering", total_tokens=10)
    usage_ledger.record_usage(source="chat", user=user, total_tokens=5)

    payload = usage_dashboards.dashboard(user_id=user.id, days=7)
    rows = {row["cost_center"]: row for row in payload["by_cost_center"]}

    assert rows["engineering"]["total_tokens"] == 10
    assert rows["unknown"]["total_tokens"] == 5
