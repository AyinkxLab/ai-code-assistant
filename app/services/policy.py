"""Bounded policy-document evaluation for sensitive actions (#600).

Policies are data, never code. A JSON object or a YAML mapping is parsed with a
bounded loader, normalized into a small rule model, and evaluated with exact
equality checks. There are no expressions, imports, calls, wildcards, regular
expressions, or custom tags, so a policy cannot execute anything.

A policy has one supported default: ``deny``. An action is allowed only when an
explicit ``allow`` rule matches; any matching ``deny`` rule wins. Unknown
actions, malformed documents, oversized documents, and invalid contexts raise a
:class:`PolicyError` with a stable, structured payload instead of leaking parser
details.

Document shape::

    version: 1
    default: deny
    rules:
      - name: allow-owners-to-change-roles
        effect: allow
        actions: [role.create, role.update, role.delete]
        when:
          roles: [owner]
      - name: deny-external-data-export
        effect: deny
        actions: [data.export]
        when:
          attributes:
            destination: external
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

try:  # PyYAML is a declared runtime dependency; keep import errors structured.
    import yaml
except ImportError:  # pragma: no cover - only when dependencies are incomplete
    yaml = None  # type: ignore[assignment]

MAX_DOCUMENT_BYTES = 64 * 1024
MAX_DEPTH = 12
MAX_TOP_LEVEL_KEYS = 32
MAX_ITEMS = 256
MAX_NODES = 2048
MAX_RULES = 200
MAX_ACTIONS_PER_RULE = 64
MAX_CONDITION_VALUES = 64
MAX_STRING_LENGTH = 256
MAX_INT_ABS = 10**12

MALFORMED_POLICY = "malformed_policy"
POLICY_TOO_LARGE = "policy_too_large"
UNKNOWN_ACTION = "unknown_action"
INVALID_CONTEXT = "invalid_context"
UNSUPPORTED_POLICY_FORMAT = "unsupported_policy_format"

#: Sensitive actions a policy may govern. Names are deliberately stable and
#: exact; adding one is an explicit API change, not a side effect of parsing.
SENSITIVE_ACTIONS = (
    "role.create",
    "role.update",
    "role.delete",
    "plugin.install",
    "plugin.policy.update",
    "audit.export",
    "data.export",
    "sso.configure",
    "scim.configure",
)
_SENSITIVE_ACTION_SET = frozenset(SENSITIVE_ACTIONS)

_TOP_LEVEL_KEYS = frozenset({"version", "default", "rules"})
_RULE_KEYS = frozenset({"name", "effect", "actions", "when"})
_WHEN_KEYS = frozenset({"roles", "workspace_ids", "user_ids", "attributes"})
_LIST_CONDITIONS = ("roles", "workspace_ids", "user_ids")


class PolicyError(Exception):
    """A structured, public error from policy parsing or evaluation."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Return the structured error payload."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


@dataclass(frozen=True)
class Rule:
    """One declarative allow or deny rule."""

    name: str
    effect: str
    actions: frozenset[str]
    when: dict[str, Any]


@dataclass(frozen=True)
class Policy:
    """A normalized, bounded policy document."""

    version: int
    default: str
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Decision:
    """The result of evaluating one action against a policy."""

    action: str
    allowed: bool
    reasons: tuple[str, ...]
    matched_rule: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the decision for logs and API responses."""
        return {
            "action": self.action,
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "matched_rule": self.matched_rule,
        }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


class _BoundedSafeLoader(yaml.SafeLoader if yaml is not None else object):  # type: ignore[misc]
    """A SafeLoader with node/key bounds; aliases and custom tags stay impossible."""

    def __init__(self, stream):  # type: ignore[no-untyped-def]
        super().__init__(stream)  # type: ignore[misc]
        self._node_count = 0

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        self._node_count += 1
        if self._node_count > MAX_NODES:
            raise PolicyError(POLICY_TOO_LARGE, "Policy has too many nodes.")
        return super().compose_node(parent, index)  # type: ignore[misc]

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        if not isinstance(node, yaml.MappingNode):
            raise PolicyError(MALFORMED_POLICY, "Expected a YAML mapping.")
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise PolicyError(MALFORMED_POLICY, "Policy keys must be strings.")
            if key in result:
                raise PolicyError(MALFORMED_POLICY, "Policy has a duplicate key.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _check_bounds(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise PolicyError(POLICY_TOO_LARGE, "Policy is nested too deeply.")
    if isinstance(value, dict):
        if len(value) > MAX_TOP_LEVEL_KEYS:
            raise PolicyError(POLICY_TOO_LARGE, "Policy object has too many keys.")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_STRING_LENGTH:
                raise PolicyError(MALFORMED_POLICY, "Policy keys must be bounded strings.")
            _check_bounds(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise PolicyError(POLICY_TOO_LARGE, "Policy list is too long.")
        for item in value:
            _check_bounds(item, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise PolicyError(POLICY_TOO_LARGE, "Policy string is too long.")
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int):
        if abs(value) > MAX_INT_ABS:
            raise PolicyError(POLICY_TOO_LARGE, "Policy integer is out of range.")
    else:
        raise PolicyError(MALFORMED_POLICY, "Policy contains an unsupported value type.")


def _load_document(document: str | bytes | dict[str, Any], fmt: str | None) -> Any:
    if isinstance(document, dict):
        _check_bounds(document)
        return document
    if not isinstance(document, (str, bytes)):
        raise PolicyError(MALFORMED_POLICY, "Policy must be a JSON/YAML document.")

    raw = document.encode("utf-8") if isinstance(document, str) else document
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise PolicyError(POLICY_TOO_LARGE, "Policy document is too large.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(
            MALFORMED_POLICY, "Policy must be UTF-8.", {"reason": "invalid_utf8"}
        ) from exc

    requested = (fmt or "auto").strip().lower()
    if requested not in {"auto", "json", "yaml", "yml"}:
        raise PolicyError(
            UNSUPPORTED_POLICY_FORMAT,
            "Policy format must be json or yaml.",
            {"format": fmt},
        )

    if requested in ("auto", "json"):
        try:
            data = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            if requested == "json":
                raise PolicyError(
                    MALFORMED_POLICY,
                    "Policy is not valid JSON.",
                    {"format": "json", "reason": str(exc)},
                ) from exc
        else:
            _check_bounds(data)
            return data

    if requested == "auto" and yaml is None:  # pragma: no cover - dependency guard
        raise PolicyError(
            UNSUPPORTED_POLICY_FORMAT,
            "YAML policies require PyYAML.",
            {"format": "yaml"},
        )
    if yaml is None:  # pragma: no cover - dependency guard
        raise PolicyError(
            UNSUPPORTED_POLICY_FORMAT, "YAML policies require PyYAML.", {"format": "yaml"}
        )
    try:
        data = yaml.load(text, Loader=_BoundedSafeLoader)
    except PolicyError:
        raise
    except yaml.YAMLError as exc:
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy is not valid YAML.",
            {"format": "yaml", "reason": type(exc).__name__},
        ) from exc
    _check_bounds(data)
    return data


def _validate_action(action: Any, *, rule_name: str) -> str:
    if not isinstance(action, str) or action not in _SENSITIVE_ACTION_SET:
        raise PolicyError(
            UNKNOWN_ACTION,
            "Policy references an unknown sensitive action.",
            {"action": action if isinstance(action, str) else None, "rule": rule_name},
        )
    return action


def _validate_condition_values(values: Any, *, rule_name: str, key: str) -> frozenset[Any]:
    if not isinstance(values, list) or not values or len(values) > MAX_CONDITION_VALUES:
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy condition must be a bounded, non-empty list.",
            {"rule": rule_name, "condition": key},
        )
    for value in values:
        if not isinstance(value, (str, int, bool)):
            raise PolicyError(
                MALFORMED_POLICY,
                "Policy condition values must be strings, integers, or booleans.",
                {"rule": rule_name, "condition": key},
            )
    return frozenset(values)


def _validate_attributes(value: Any, *, rule_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or len(value) > MAX_CONDITION_VALUES:
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy attributes condition must be a bounded, non-empty mapping.",
            {"rule": rule_name, "condition": "attributes"},
        )
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, bool)):
            raise PolicyError(
                MALFORMED_POLICY,
                "Policy attributes must map strings to scalar values.",
                {"rule": rule_name, "condition": "attributes"},
            )
    return dict(value)


def _validate_rule(value: Any, index: int) -> Rule:
    if not isinstance(value, dict):
        raise PolicyError(MALFORMED_POLICY, "Each policy rule must be an object.")
    if not set(value) <= _RULE_KEYS:
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy rule has an unknown field.",
            {"rule": index, "fields": sorted(set(value) - _RULE_KEYS)},
        )
    name = value.get("name") or f"rule-{index}"
    if not isinstance(name, str) or not name.strip():
        raise PolicyError(MALFORMED_POLICY, "Policy rule name must be a non-empty string.")
    name = name.strip()

    effect = value.get("effect")
    if effect not in ("allow", "deny"):
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy rule effect must be allow or deny.",
            {"rule": name},
        )

    actions_value = value.get("actions")
    if (
        not isinstance(actions_value, list)
        or not actions_value
        or len(actions_value) > MAX_ACTIONS_PER_RULE
    ):
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy rule actions must be a bounded, non-empty list.",
            {"rule": name},
        )
    actions = frozenset(_validate_action(item, rule_name=name) for item in actions_value)

    when: dict[str, Any] = {}
    raw_when = value.get("when", {})
    if not isinstance(raw_when, dict) or not set(raw_when) <= _WHEN_KEYS:
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy rule condition is empty or uses an unknown field.",
            {"rule": name},
        )
    for key, item in raw_when.items():
        if key == "attributes":
            when[key] = _validate_attributes(item, rule_name=name)
        else:
            when[key] = _validate_condition_values(item, rule_name=name, key=key)
    return Rule(name=name, effect=effect, actions=actions, when=when)


def parse_policy(document: str | bytes | dict[str, Any], *, fmt: str | None = None) -> Policy:
    """Parse and validate a bounded JSON or YAML policy document."""
    data = _load_document(document, fmt)
    if not isinstance(data, dict) or set(data) != _TOP_LEVEL_KEYS:
        raise PolicyError(
            MALFORMED_POLICY,
            "Policy must contain exactly version, default, and rules.",
        )
    if data["version"] != 1:
        raise PolicyError(
            MALFORMED_POLICY, "Policy version must be 1.", {"version": data["version"]}
        )
    if data["default"] != "deny":
        raise PolicyError(
            MALFORMED_POLICY,
            "Only deny-by-default policies are supported.",
            {"default": data["default"]},
        )
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
        raise PolicyError(POLICY_TOO_LARGE, "Policy rule count is out of range.")
    if not raw_rules:
        raise PolicyError(MALFORMED_POLICY, "Policy must define at least one rule.")
    rules = tuple(_validate_rule(item, index) for index, item in enumerate(raw_rules, start=1))
    return Policy(version=1, default="deny", rules=rules)


def _context_attributes(context: dict[str, Any]) -> dict[str, Any]:
    attributes = context.get("attributes", {})
    if not isinstance(attributes, dict):
        raise PolicyError(
            INVALID_CONTEXT, "Context attributes must be an object.", {"field": "attributes"}
        )
    return attributes


def _matches(rule: Rule, context: dict[str, Any]) -> bool:
    attributes = _context_attributes(context) if "attributes" in rule.when else {}
    for key in _LIST_CONDITIONS:
        if key not in rule.when:
            continue
        actual = context.get(key)
        if isinstance(actual, (list, tuple, set, frozenset)):
            if not any(
                isinstance(item, (str, int, bool)) and item in rule.when[key] for item in actual
            ):
                return False
        elif not isinstance(actual, (str, int, bool)) or actual not in rule.when[key]:
            return False
    if "attributes" in rule.when:
        for key, expected in rule.when["attributes"].items():
            if attributes.get(key) != expected:
                return False
    return True


def evaluate(policy: Policy, action: str, *, context: dict[str, Any] | None = None) -> Decision:
    """Evaluate one sensitive action, returning an explicit reason for the result."""
    _validate_action(action, rule_name="evaluation")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise PolicyError(INVALID_CONTEXT, "Evaluation context must be an object.")

    allowed = False
    reasons: list[str] = []
    matched_rule: str | None = None
    for rule in policy.rules:
        if action not in rule.actions or not _matches(rule, context):
            continue
        if rule.effect == "deny":
            return Decision(
                action=action,
                allowed=False,
                reasons=(f"denied by rule {rule.name!r}",),
                matched_rule=rule.name,
            )
        allowed = True
        matched_rule = rule.name
        reasons.append(f"allowed by rule {rule.name!r}")

    if not allowed:
        return Decision(
            action=action, allowed=False, reasons=("deny_by_default",), matched_rule=None
        )
    return Decision(
        action=action,
        allowed=True,
        reasons=tuple(reasons),
        matched_rule=matched_rule,
    )


def evaluate_document(
    document: str | bytes | dict[str, Any],
    action: str,
    *,
    fmt: str | None = None,
    context: dict[str, Any] | None = None,
) -> Decision:
    """Convenience helper: parse a document and evaluate one action."""
    return evaluate(parse_policy(document, fmt=fmt), action, context=context)


__all__ = [
    "MALFORMED_POLICY",
    "MAX_DOCUMENT_BYTES",
    "POLICY_TOO_LARGE",
    "SENSITIVE_ACTIONS",
    "UNKNOWN_ACTION",
    "Decision",
    "Policy",
    "PolicyError",
    "Rule",
    "evaluate",
    "evaluate_document",
    "parse_policy",
]
