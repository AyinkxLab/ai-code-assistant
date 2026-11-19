"""Query profiling and slow-query log guards (#696)."""

from __future__ import annotations

import logging

from sqlalchemy import event, text

from app.models import Conversation, Message
from app.observability.query_profiling import (
    get_query_count,
    statement_shape,
)


def test_statement_shape_strips_string_and_numeric_literals():
    shape = statement_shape(
        "SELECT * FROM users WHERE email = 'person@example.com' AND id = 42 "
        "AND name = 'O''Brien'"
    )
    assert "person@example.com" not in shape
    assert "42" not in shape
    assert "O''Brien" not in shape
    assert shape == "SELECT * FROM users WHERE email = ? AND id = ? AND name = ?"


def test_statement_shape_is_bounded():
    assert len(statement_shape("SELECT " + "x" * 500)) <= 200


def test_request_query_count_increments(app, db):
    with app.test_request_context("/"):
        db.session.execute(text("SELECT 1"))
        db.session.execute(text("SELECT 2"))
        assert get_query_count() >= 2


def test_slow_query_log_does_not_include_bound_parameters(app, db, caplog):
    app.config["QUERY_SLOW_THRESHOLD_MS"] = 0
    with (
        caplog.at_level(logging.WARNING, logger="app.observability.query"),
        app.test_request_context("/admin/audit"),
    ):
        db.session.execute(text("SELECT :secret"), {"secret": "hunter2-do-not-log"})
        db.session.commit()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "slow_query" in messages
    assert "hunter2-do-not-log" not in messages
    assert "SELECT ?" in messages


def test_conversation_list_does_not_regress_to_n_plus_one(client, app, db, make_user, login):
    user = make_user(username="profiled", email="profiled@example.com")
    login(email="profiled@example.com")
    for index in range(4):
        conversation = Conversation(user_id=user.id, title=f"Conversation {index}")
        db.session.add(conversation)
        db.session.flush()
        db.session.add_all(
            [
                Message(conversation_id=conversation.id, role="user", content="hello"),
                Message(conversation_id=conversation.id, role="assistant", content="hi"),
            ]
        )
    db.session.commit()

    statements: list[str] = []

    def _count(*_args, **_kwargs):
        statements.append("query")

    event.listen(db.engine, "before_cursor_execute", _count)
    try:
        response = client.get("/api/conversations")
    finally:
        event.remove(db.engine, "before_cursor_execute", _count)

    assert response.status_code == 200
    assert len(response.get_json()) == 4
    # One query for the user, one for conversations, one each for the eager
    # message/share loads. Without eager loading this is 1 + 1 + 8.
    assert len(statements) <= 8, len(statements)
