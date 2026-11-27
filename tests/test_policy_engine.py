"""Tests for the bounded JSON/YAML policy engine (#600)."""

from __future__ import annotations

import json

import pytest

from app.services import policy


def _document(*rules: dict, default: str = "deny") -> dict:
    return {"version": 1, "default": default, "rules": list(rules)}


def _rule(
    name: str,
    effect: str,
    actions: list[str],
    when: dict | None = None,
) -> dict:
    rule = {"name": name, "effect": effect, "actions": actions}
    if when is not None:
        rule["when"] = when
    return rule


class TestEvaluation:
    def test_json_policy_allows_with_a_reason(self):
        document = json.dumps(
            _document(
                _rule("allow-owners", "allow", ["role.create"], {"roles": ["owner"]}),
            )
        )

        decision = policy.evaluate_document(
            document, "role.create", fmt="json", context={"roles": ["owner"]}
        )

        assert decision.allowed is True
        assert decision.matched_rule == "allow-owners"
        assert decision.reasons == ("allowed by rule 'allow-owners'",)
        assert decision.to_dict() == {
            "action": "role.create",
            "allowed": True,
            "reasons": ["allowed by rule 'allow-owners'"],
            "matched_rule": "allow-owners",
        }

    def test_yaml_policy_is_parsed_as_data(self):
        document = """\
version: 1
default: deny
rules:
  - name: allow-install
    effect: allow
    actions: [plugin.install]
    when:
      roles: [owner]
"""

        decision = policy.evaluate_document(
            document, "plugin.install", fmt="yaml", context={"roles": ["owner"]}
        )

        assert decision.allowed is True
        assert decision.matched_rule == "allow-install"

    def test_deny_by_default_when_no_rule_matches(self):
        parsed = policy.parse_policy(_document(_rule("allow", "allow", ["role.update"])))

        decision = policy.evaluate(parsed, "role.delete")

        assert decision.allowed is False
        assert decision.reasons == ("deny_by_default",)
        assert decision.matched_rule is None

    def test_condition_mismatch_denies_by_default(self):
        parsed = policy.parse_policy(
            _document(_rule("owners", "allow", ["role.update"], {"roles": ["owner"]}))
        )

        decision = policy.evaluate(parsed, "role.update", context={"roles": ["viewer"]})

        assert decision.allowed is False
        assert decision.reasons == ("deny_by_default",)

    def test_deny_rule_overrides_an_allow_rule(self):
        parsed = policy.parse_policy(
            _document(
                _rule("allow-export", "allow", ["data.export"]),
                _rule(
                    "deny-external",
                    "deny",
                    ["data.export"],
                    {"attributes": {"destination": "external"}},
                ),
            )
        )

        decision = policy.evaluate(
            parsed, "data.export", context={"attributes": {"destination": "external"}}
        )

        assert decision.allowed is False
        assert decision.matched_rule == "deny-external"
        assert decision.reasons == ("denied by rule 'deny-external'",)

    def test_deny_is_evaluated_first_and_still_wins(self):
        parsed = policy.parse_policy(
            _document(
                _rule("deny-first", "deny", ["audit.export"]),
                _rule("allow-second", "allow", ["audit.export"]),
            )
        )

        decision = policy.evaluate(parsed, "audit.export")

        assert decision.allowed is False
        assert decision.matched_rule == "deny-first"


class TestStructuredErrors:
    def test_unknown_evaluation_action(self):
        parsed = policy.parse_policy(_document(_rule("allow", "allow", ["role.create"])))

        with pytest.raises(policy.PolicyError) as excinfo:
            policy.evaluate(parsed, "system.reboot")

        assert excinfo.value.code == policy.UNKNOWN_ACTION
        assert excinfo.value.to_dict()["error"]["details"]["action"] == "system.reboot"

    def test_unknown_action_inside_policy(self):
        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy(_document(_rule("allow", "allow", ["role.nuke"])))

        assert excinfo.value.code == policy.UNKNOWN_ACTION
        assert excinfo.value.details == {"action": "role.nuke", "rule": "allow"}

    def test_malformed_json_is_structured(self):
        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy("{not json", fmt="json")

        assert excinfo.value.code == policy.MALFORMED_POLICY
        assert excinfo.value.details["format"] == "json"

    def test_malformed_yaml_is_structured(self):
        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy("version: [1\n", fmt="yaml")

        assert excinfo.value.code == policy.MALFORMED_POLICY
        assert excinfo.value.details["format"] == "yaml"

    def test_custom_yaml_tags_are_rejected_without_execution(self, tmp_path):
        marker = tmp_path / "executed"
        document = f'!!python/object/apply:builtins.open ["{marker.as_posix()}", "w"]'

        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy(document, fmt="yaml")

        assert excinfo.value.code == policy.MALFORMED_POLICY
        assert excinfo.value.details["reason"] in {
            "ConstructorError",
            "ParserError",
            "ScannerError",
        }
        assert not marker.exists()

    def test_duplicate_json_keys_are_rejected(self):
        document = '{"version": 1, "version": 2, "default": "deny", "rules": []}'

        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy(document, fmt="json")

        assert excinfo.value.code == policy.MALFORMED_POLICY

    def test_unknown_condition_field_is_rejected(self):
        document = _document(_rule("allow", "allow", ["role.create"], {"unknown": ["owner"]}))

        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy(document)

        assert excinfo.value.code == policy.MALFORMED_POLICY

    def test_invalid_context_is_structured(self):
        parsed = policy.parse_policy(_document(_rule("allow", "allow", ["role.create"])))

        with pytest.raises(policy.PolicyError) as excinfo:
            policy.evaluate(parsed, "role.create", context=["owner"])

        assert excinfo.value.code == policy.INVALID_CONTEXT


class TestBounds:
    def test_oversized_document_is_rejected(self):
        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy("x" * (policy.MAX_DOCUMENT_BYTES + 1))

        assert excinfo.value.code == policy.POLICY_TOO_LARGE

    def test_deeply_nested_document_is_rejected(self):
        nested: object = "leaf"
        for _ in range(policy.MAX_DEPTH + 2):
            nested = [nested]

        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy({"version": 1, "default": "deny", "rules": nested})

        assert excinfo.value.code == policy.POLICY_TOO_LARGE

    def test_too_many_rules_are_rejected(self):
        rules = [
            _rule(f"r-{index}", "allow", ["role.create"]) for index in range(policy.MAX_RULES + 1)
        ]

        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy(_document(*rules))

        assert excinfo.value.code == policy.POLICY_TOO_LARGE

    def test_allow_by_default_is_rejected(self):
        with pytest.raises(policy.PolicyError) as excinfo:
            policy.parse_policy(_document(default="allow"))

        assert excinfo.value.code == policy.MALFORMED_POLICY
        assert excinfo.value.details["default"] == "allow"
