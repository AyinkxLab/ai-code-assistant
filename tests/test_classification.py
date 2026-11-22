"""Tests for data classification labels and their export/log enforcement (#612)."""

import logging

import pytest

from app.extensions import db
from app.models import Conversation, Message
from app.services import chat_audit, classification
from app.services.chat_export import render_conversation_markdown


class TestClassificationLabels:
    def test_levels_are_ordered(self):
        assert classification.RANK["public"] < classification.RANK["internal"]
        assert classification.RANK["internal"] < classification.RANK["confidential"]
        assert classification.RANK["confidential"] < classification.RANK["pii"]

    def test_known_fields_are_classified(self):
        assert classification.level_for("id") == classification.PUBLIC
        assert classification.level_for("name") == classification.INTERNAL
        assert classification.level_for("content") == classification.CONFIDENTIAL
        assert classification.level_for("email") == classification.PII

    def test_unknown_fields_default_to_internal(self):
        assert classification.level_for("something_new") == classification.INTERNAL

    def test_invalid_level_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown classification"):
            classification.validate_level("top-secret")


class TestExportProjection:
    def test_confidential_fields_never_appear_in_a_public_export(self):
        record = {
            "id": 7,
            "action": "user_created",
            "name": "Project",
            "content": "internal source code",
            "metadata": {"note": "secret"},
            "email": "person@example.com",
            "username": "person",
        }

        result = classification.public_export(record)

        assert result == {"id": 7, "action": "user_created"}
        for key in ("content", "metadata", "email", "username", "name"):
            assert key not in result

    def test_internal_export_excludes_pii_and_content(self):
        record = {"id": 1, "created_at": "2026-01-01", "email": "a@example.com", "body": "x"}

        result = classification.export_record(record, max_level=classification.INTERNAL)

        assert result == {"id": 1, "created_at": "2026-01-01"}

    def test_public_export_rows_projects_each_record(self):
        rows = [{"id": 1, "content": "x"}, {"id": 2, "email": "b@example.com"}]

        assert classification.public_export_rows(rows) == [{"id": 1}, {"id": 2}]


class TestLogRedaction:
    def test_sensitive_fields_are_redacted(self):
        result = classification.redact_for_log(
            {"status": "ok", "user_id": 3, "content": "secret", "email": "a@example.com"}
        )

        assert result["status"] == "ok"
        assert result["user_id"] == 3
        assert result["content"] == "[redacted:confidential]"
        assert result["email"] == "[redacted:pii]"

    def test_log_event_applies_redaction(self):
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record)

        handler = _Capture()
        previous_level = chat_audit.LOGGER.level
        chat_audit.LOGGER.setLevel(logging.DEBUG)
        chat_audit.LOGGER.disabled = False
        chat_audit.LOGGER.addHandler(handler)
        try:
            chat_audit.log_event(
                "test.event",
                content="top secret",
                email="person@example.com",
                status="ok",
            )
        finally:
            chat_audit.LOGGER.removeHandler(handler)
            chat_audit.LOGGER.setLevel(previous_level)

        assert captured
        fields = captured[0].audit_fields
        assert fields["content"] == "[redacted:confidential]"
        assert fields["email"] == "[redacted:pii]"
        assert fields["status"] == "ok"


class TestConversationExportClassification:
    def _conversation(self, make_user):
        user = make_user()
        conversation = Conversation(user_id=user.id, title="Private plan")
        db.session.add(conversation)
        db.session.flush()
        conversation.messages.append(Message(role="user", content="TOP SECRET"))
        db.session.commit()
        return conversation

    def test_default_export_is_labelled_confidential_and_keeps_content(self, db, make_user):
        conversation = self._conversation(make_user)

        body = render_conversation_markdown(conversation)

        assert "Classification:** confidential" in body
        assert "TOP SECRET" in body

    def test_public_export_withholds_confidential_content_and_title(self, db, make_user):
        conversation = self._conversation(make_user)

        body = render_conversation_markdown(conversation, max_level=classification.PUBLIC)

        assert "TOP SECRET" not in body
        assert "Private plan" not in body
        assert "[redacted:internal]" in body
        assert "withheld" in body.lower()
