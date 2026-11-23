"""Central PII redaction for logs and exports (#613)."""

from __future__ import annotations

from app.services import classification


class TestFieldList:
    def test_documented_field_list_covers_pii_classification(self):
        for field in ("email", "username", "token", "api_key", "secret", "password"):
            assert field in classification.REDACT_FIELDS

    def test_unknown_fields_are_not_redacted(self):
        assert "status" not in classification.REDACT_FIELDS


class TestRecursiveRedaction:
    def test_nested_dicts_and_lists_are_redacted(self):
        payload = {
            "status": "ok",
            "user": {
                "email": "person@example.com",
                "profile": {"display_name": "Person", "note": "write to other@example.com"},
            },
            "tokens": [{"token": "secret-token"}, "plain"],
        }

        result = classification.redact_for_log(payload)

        assert result["status"] == "ok"
        assert result["user"]["email"] == "[redacted:pii]"
        assert result["user"]["profile"]["display_name"] == "[redacted:pii]"
        assert result["user"]["profile"]["note"] == "write to [redacted:email]"
        assert result["tokens"][0]["token"] == "[redacted:pii]"
        assert result["tokens"][1] == "plain"

    def test_original_payload_is_not_mutated(self):
        payload = {"email": "person@example.com"}
        classification.redact_for_log(payload)
        assert payload == {"email": "person@example.com"}

    def test_redact_text_replaces_embedded_emails(self):
        text = "contact a@example.com or b.c+d@sub.example.org"
        assert "[redacted:email]" in classification.redact_text(text)
        assert "a@example.com" not in classification.redact_text(text)
        assert classification.redact_text("no address here") == "no address here"

    def test_redact_alias_uses_pii_ceiling(self):
        result = classification.redact({"note": "x", "email": "a@example.com"})
        assert result["note"] == "x"
        assert result["email"] == "[redacted:pii]"


class TestLogPath:
    def test_log_event_redacts_nested_payloads(self):
        assert classification.redact_for_log({"outer": {"email": "a@example.com"}}) == {
            "outer": {"email": "[redacted:pii]"}
        }


class TestExportPath:
    def test_audit_export_row_is_redacted(self, db, make_user):
        from app.services import audit, audit_export

        user = make_user()
        entry = audit.record(
            audit.LOGIN_FAILURE,
            user=user,
            metadata={"note": "contact admin@example.com"},
        )
        db.session.commit()

        row = audit_export.export_row(entry)

        assert "admin@example.com" not in str(row)
        assert row["action"] == audit.LOGIN_FAILURE
