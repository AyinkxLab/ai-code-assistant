"""Top conversations and projects by usage cost (#452)."""

from app.services import usage_ledger


def _record(app, user_id, *, conversation_id, project_id, tokens, cost):
    with app.app_context():
        usage_ledger.record_usage(
            user_id=user_id,
            conversation_id=conversation_id,
            project_id=project_id,
            source="chat",
            total_tokens=tokens,
            cost_estimate=cost,
            commit=True,
        )


class TestUsageTop:
    def test_orders_by_cost_then_tokens_and_respects_limit(self, app, make_user):
        user = make_user()
        _record(app, user.id, conversation_id=1, project_id=10, tokens=100, cost=1.0)
        _record(app, user.id, conversation_id=1, project_id=10, tokens=100, cost=1.0)
        _record(app, user.id, conversation_id=2, project_id=20, tokens=500, cost=5.0)
        _record(app, user.id, conversation_id=3, project_id=30, tokens=900, cost=3.0)

        with app.app_context():
            top = usage_ledger.top_entities(user_id=user.id, limit=2)
        assert top["limit"] == 2
        # Conversation 2 costs 5.0, conversation 3 costs 3.0 and conversation 1
        # aggregates two records for 2.0, so cost decides the top two.
        assert [row["id"] for row in top["conversations"]] == [2, 3]
        assert [row["id"] for row in top["projects"]] == [20, 30]

        # Without truncation, conversation 1 is present and its two records are
        # aggregated into one row.
        with app.app_context():
            all_top = usage_ledger.top_entities(user_id=user.id, limit=3)
        assert [row["id"] for row in all_top["conversations"]] == [2, 3, 1]
        assert all_top["conversations"][2]["records"] == 2
        assert all_top["conversations"][2]["total_tokens"] == 200

    def test_scoped_to_the_requesting_user(self, app, make_user):
        owner = make_user(email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        _record(app, owner.id, conversation_id=1, project_id=1, tokens=1, cost=1.0)
        _record(app, other.id, conversation_id=2, project_id=2, tokens=999, cost=999.0)

        with app.app_context():
            top = usage_ledger.top_entities(user_id=owner.id)
        assert [row["id"] for row in top["conversations"]] == [1]
        assert [row["id"] for row in top["projects"]] == [1]

    def test_limit_is_bounded(self, app, make_user):
        user = make_user()
        _record(app, user.id, conversation_id=1, project_id=1, tokens=1, cost=1.0)
        with app.app_context():
            top = usage_ledger.top_entities(user_id=user.id, limit=10_000)
        assert top["limit"] == usage_ledger.TOP_MAX_LIMIT

    def test_api_is_authenticated_and_owner_scoped(self, app, client, make_user, login):
        user = make_user()
        _record(app, user.id, conversation_id=7, project_id=7, tokens=10, cost=2.0)
        login()
        response = client.get("/api/usage/top?limit=1")
        assert response.status_code == 200
        assert response.get_json()["conversations"][0]["id"] == 7
