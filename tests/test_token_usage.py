"""Tests for token usage tracking and daily aggregation (issue #13)."""

import json
from datetime import datetime, timedelta

from app.models import Conversation, Message
from app.services import token_usage


def _register(client, username="usageuser", email="usageuser@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _create_conversation(client, title=None):
    payload = {"title": title} if title is not None else {}
    response = client.post("/chat/conversations", json=payload, headers={"X-CSRFToken": "ignored"})
    assert response.status_code == 201
    return response.get_json()


def _send(client, conversation_id, content="Explain the strategy pattern"):
    return client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": content},
        headers={"X-CSRFToken": "ignored"},
    )


class TestEstimateTokens:
    def test_boundaries(self):
        assert token_usage.estimate_tokens("") == 0
        assert token_usage.estimate_tokens(None) == 0
        assert token_usage.estimate_tokens("abcd") == 1
        assert token_usage.estimate_tokens("a" * 401) == 101

    def test_usage_from_text(self):
        usage = token_usage.usage_from_text("a" * 400, "b" * 200)
        assert usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    def test_sum_usage_ignores_rows_without_usage(self, app):
        with app.app_context():
            conversation = Conversation(user_id=1, title="c")
            conversation.messages = [
                Message(role="user", content="hi"),
                Message(
                    role="assistant",
                    content="hello",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                ),
            ]
            assert token_usage.sum_usage(conversation.messages) == {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }


class TestMessageUsageModel:
    def test_to_dict_includes_usage(self, app):
        with app.app_context():
            message = Message(
                role="assistant",
                content="hi",
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
            )
            assert message.to_dict()["token_usage"] == {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            }

    def test_to_dict_usage_is_none_without_counts(self, app):
        with app.app_context():
            message = Message(role="user", content="hi")
            assert message.to_dict()["token_usage"] is None


class TestUsageRecorded:
    def test_non_stream_message_records_usage(self, client, db):
        _register(client)
        conversation = _create_conversation(client)

        response = _send(client, conversation["id"])

        assert response.status_code == 201
        usage = response.get_json()["assistant_message"]["token_usage"]
        assert usage and usage["total_tokens"] > 0
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

        stored = (
            Message.query.filter_by(conversation_id=conversation["id"], role="assistant")
            .order_by(Message.id.desc())
            .first()
        )
        assert stored.total_tokens == usage["total_tokens"]

    def test_stream_message_records_usage(self, client, db):
        _register(client)
        conversation = _create_conversation(client)

        response = client.post(
            f"/chat/conversations/{conversation['id']}/stream",
            json={"content": "Write a haiku about tests"},
            headers={"X-CSRFToken": "ignored"},
        )
        data = response.get_data(as_text=True)

        done = [line for line in data.splitlines() if line.startswith("data: ")]
        payloads = [json.loads(line[6:]) for line in done]
        final = [p for p in payloads if p.get("type") == "done"]
        assert final and final[0]["message"]["token_usage"]["total_tokens"] > 0

        stored = (
            Message.query.filter_by(conversation_id=conversation["id"], role="assistant")
            .order_by(Message.id.desc())
            .first()
        )
        assert stored.total_tokens is not None and stored.total_tokens > 0


class TestConversationRollup:
    def test_get_conversation_reports_cumulative_usage(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        first = _send(client, conversation["id"]).get_json()["assistant_message"]
        second = _send(client, conversation["id"], "And its trade-offs?").get_json()[
            "assistant_message"
        ]

        payload = client.get(f"/chat/conversations/{conversation['id']}").get_json()

        expected = first["token_usage"]["total_tokens"] + second["token_usage"]["total_tokens"]
        assert payload["usage"]["total_tokens"] == expected


class TestDailyUsageEndpoint:
    def test_daily_totals_are_queryable(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        message = _send(client, conversation["id"]).get_json()["assistant_message"]

        rows = client.get("/chat/api/usage/daily").get_json()

        assert len(rows) == 1
        assert rows[0]["total_tokens"] == message["token_usage"]["total_tokens"]

    def test_daily_totals_are_owner_scoped(self, client, db):
        _register(client, username="owner", email="owner@example.com")
        conversation = _create_conversation(client)
        _send(client, conversation["id"])

        client.post("/auth/logout")
        _register(client, username="other", email="other@example.com")

        assert client.get("/chat/api/usage/daily").get_json() == []

    def test_daily_requires_login(self, client):
        assert client.get("/chat/api/usage/daily").status_code == 302


class TestUsageDashboard:
    def test_usage_page_requires_login(self, client):
        response = client.get("/usage")
        assert response.status_code == 302

    def test_usage_page_renders_for_logged_in_user(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        _send(client, conversation["id"])

        response = client.get("/usage")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Usage" in body

    def test_usage_summary_endpoint_requires_login(self, client):
        assert client.get("/chat/api/usage/summary").status_code == 302

    def test_usage_summary_reports_current_month_totals(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        message = _send(client, conversation["id"]).get_json()["assistant_message"]

        payload = client.get("/chat/api/usage/summary").get_json()

        assert payload["total_tokens"] == message["token_usage"]["total_tokens"]
        assert payload["estimated_cost"] >= 0
        assert "prompt_tokens" in payload
        assert "completion_tokens" in payload

    def test_usage_summary_is_owner_scoped(self, client, db):
        _register(client, username="owner2", email="owner2@example.com")
        conversation = _create_conversation(client)
        _send(client, conversation["id"])

        client.post("/auth/logout")
        _register(client, username="other2", email="other2@example.com")

        payload = client.get("/chat/api/usage/summary").get_json()

        assert payload["total_tokens"] == 0

    def test_usage_summary_includes_daily_series(self, client, db):
        _register(client)
        conversation = _create_conversation(client)
        _send(client, conversation["id"])

        payload = client.get("/chat/api/usage/summary").get_json()

        assert "daily" in payload
        assert len(payload["daily"]) == 30
        today = datetime.utcnow().date()
        dates = [row["date"] for row in payload["daily"]]
        assert today.isoformat() in dates
        assert (today - timedelta(days=29)).isoformat() in dates
