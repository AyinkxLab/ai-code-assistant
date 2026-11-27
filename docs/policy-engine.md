# Bounded policy documents

Phase 11 evaluates policies for sensitive actions with `app/services/policy.py`.
A policy is **data only**: JSON objects and YAML mappings are parsed with strict
bounds and evaluated with exact equality checks. There are no expressions,
imports, function calls, wildcards, regular expressions, or custom YAML tags, so
a policy cannot execute code.

## Document shape

```yaml
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
```

The same document may be supplied as JSON. Required top-level keys are exactly
`version`, `default`, and `rules`; `version` must be `1` and `default` must be
`deny`. There is no allow-by-default mode.

Each rule has:

| Field | Meaning |
| --- | --- |
| `name` | Stable, non-empty identifier used in decision reasons. |
| `effect` | `allow` or `deny`. |
| `actions` | Non-empty list of exact sensitive action names. |
| `when` | Optional equality conditions; all listed conditions must match. |

Supported `when` keys are `roles`, `workspace_ids`, `user_ids`, and
`attributes`. The first three mean “context value is one of these values”;
`attributes` maps context attribute names to scalar values. Unknown keys are
rejected.

Supported actions are: `role.create`, `role.update`, `role.delete`,
`plugin.install`, `plugin.policy.update`, `audit.export`, `data.export`,
`sso.configure`, and `scim.configure`.

## Evaluation

```python
from app.services import policy

decision = policy.evaluate_document(
    document,
    "role.create",
    fmt="yaml",
    context={"roles": ["owner"]},
)
assert decision.allowed
assert decision.reasons == ("allowed by rule 'allow-owners-to-change-roles'",)
```

Rules are checked in document order:

1. An explicit matching `deny` wins immediately.
2. Matching `allow` rules set `allowed=True` and contribute reasons.
3. No match returns `allowed=False` with reason `deny_by_default`.

`Decision.to_dict()` returns `action`, `allowed`, `reasons`, and `matched_rule`
for structured logs and API responses.

## Limits and errors

Documents are bounded by 64 KiB, 12 levels of nesting, 200 rules, 64 actions per
rule, 64 values per condition, and 256 characters per string. YAML uses a
`SafeLoader` subclass that also caps node count and rejects duplicate keys.
JSON duplicate keys are rejected too. Invalid input raises `PolicyError` with a
stable code and a safe detail object; parser exception text is never exposed to
callers.

| Code | Meaning |
| --- | --- |
| `malformed_policy` | Invalid shape, unknown field, duplicate key, or rejected YAML tag. |
| `policy_too_large` | A byte, depth, node, list, rule, or string bound was exceeded. |
| `unknown_action` | The action or a policy rule name is not in `SENSITIVE_ACTIONS`. |
| `invalid_context` | The evaluation context is not an object, or attributes are not an object. |
| `unsupported_policy_format` | The requested format is not `json`, `yaml`, or `auto`. |

Example structured error:

```json
{"error": {"code": "unknown_action", "message": "Policy references an unknown sensitive action.", "details": {"action": "role.nuke", "rule": "allow"}}}
```

## Tests

`tests/test_policy_engine.py` covers JSON and YAML allow paths, deny precedence,
deny-by-default, malformed and oversized documents, unknown actions, duplicate
keys, rejected YAML tags, and invalid contexts:

```console
python -m pytest tests/test_policy_engine.py -o addopts="" -q
```
