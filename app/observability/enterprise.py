"""Bounded telemetry for enterprise feature adoption (#645).

Only fixed, documented enum values are accepted as labels. Identifiers such as
workspace ids, user ids, emails, domains, and configuration secrets are never
used as label values; unknown values collapse to ``other`` so cardinality stays
bounded even if a caller supplies hostile input.

The canonical metric is ``enterprise_feature_events_total``. Per-feature
families make targeted dashboards and alerts easy without widening the label
set. Audit exports additionally carry a bounded ``format`` label.
"""

from __future__ import annotations

from .metrics import REGISTRY

FEATURE_SSO = "sso"
FEATURE_SCIM = "scim"
FEATURE_CUSTOM_ROLES = "custom_roles"
FEATURE_AUDIT_EXPORT = "audit_export"
FEATURES = (FEATURE_SSO, FEATURE_SCIM, FEATURE_CUSTOM_ROLES, FEATURE_AUDIT_EXPORT)

ACTION_CHALLENGE = "challenge"
ACTION_VERIFY = "verify"
ACTION_ENFORCE = "enforce"
ACTION_DISABLE = "disable"
ACTION_PROVISION = "provision"
ACTION_DEPROVISION = "deprovision"
ACTION_ASSIGN = "assign"
ACTION_REVOKE = "revoke"
ACTION_EXPORT = "export"
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTION_USE = "use"
ACTION_LOGIN = "login"
ACTION_LOGOUT = "logout"
ACTION_SYNC = "sync"
ACTIONS = (
    ACTION_CHALLENGE,
    ACTION_VERIFY,
    ACTION_ENFORCE,
    ACTION_DISABLE,
    ACTION_PROVISION,
    ACTION_DEPROVISION,
    ACTION_ASSIGN,
    ACTION_REVOKE,
    ACTION_EXPORT,
    ACTION_CREATE,
    ACTION_UPDATE,
    ACTION_DELETE,
    ACTION_USE,
    ACTION_LOGIN,
    ACTION_LOGOUT,
    ACTION_SYNC,
)

OUTCOME_SUCCESS = "success"
OUTCOME_DENIED = "denied"
OUTCOME_INVALID = "invalid"
OUTCOME_ERROR = "error"
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_DENIED, OUTCOME_INVALID, OUTCOME_ERROR)

FORMAT_JSON = "json"
FORMAT_CSV = "csv"
EXPORT_FORMATS = (FORMAT_JSON, FORMAT_CSV, "other")

OVERFLOW = "other"

ENTERPRISE_FEATURE_EVENTS = REGISTRY.counter(
    "enterprise_feature_events_total",
    "Enterprise feature adoption events by feature, action and outcome.",
    ("feature", "action", "outcome"),
)
SSO_EVENTS = REGISTRY.counter(
    "enterprise_sso_events_total",
    "SSO/domain-verification events by action and outcome.",
    ("action", "outcome"),
)
SCIM_EVENTS = REGISTRY.counter(
    "enterprise_scim_events_total",
    "SCIM provisioning events by action and outcome.",
    ("action", "outcome"),
)
CUSTOM_ROLE_EVENTS = REGISTRY.counter(
    "enterprise_custom_role_events_total",
    "Custom role events by action and outcome.",
    ("action", "outcome"),
)
AUDIT_EXPORT_EVENTS = REGISTRY.counter(
    "enterprise_audit_export_events_total",
    "Audit export events by format and outcome.",
    ("format", "outcome"),
)

_SPECIFIC = {
    FEATURE_SSO: SSO_EVENTS,
    FEATURE_SCIM: SCIM_EVENTS,
    FEATURE_CUSTOM_ROLES: CUSTOM_ROLE_EVENTS,
}


def _bounded(value: object, allowed: tuple[str, ...]) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return text if text in allowed else OVERFLOW


def record_enterprise_event(feature: object, action: object, outcome: object = OUTCOME_SUCCESS):
    """Record one adoption event with fixed, sanitized labels."""
    normalized_feature = _bounded(feature, FEATURES)
    normalized_action = _bounded(action, ACTIONS)
    normalized_outcome = _bounded(outcome, OUTCOMES)
    ENTERPRISE_FEATURE_EVENTS.inc(
        feature=normalized_feature,
        action=normalized_action,
        outcome=normalized_outcome,
    )
    specific = _SPECIFIC.get(normalized_feature)
    if specific is not None:
        specific.inc(action=normalized_action, outcome=normalized_outcome)
    return normalized_feature


def record_sso_event(action: object, outcome: object = OUTCOME_SUCCESS) -> None:
    record_enterprise_event(FEATURE_SSO, action, outcome)


def record_scim_event(action: object, outcome: object = OUTCOME_SUCCESS) -> None:
    record_enterprise_event(FEATURE_SCIM, action, outcome)


def record_custom_role_event(action: object, outcome: object = OUTCOME_SUCCESS) -> None:
    record_enterprise_event(FEATURE_CUSTOM_ROLES, action, outcome)


def record_audit_export(format: object = FORMAT_JSON, outcome: object = OUTCOME_SUCCESS) -> None:
    normalized_format = _bounded(format, EXPORT_FORMATS)
    normalized_outcome = _bounded(outcome, OUTCOMES)
    ENTERPRISE_FEATURE_EVENTS.inc(
        feature=FEATURE_AUDIT_EXPORT,
        action=ACTION_EXPORT,
        outcome=normalized_outcome,
    )
    AUDIT_EXPORT_EVENTS.inc(format=normalized_format, outcome=normalized_outcome)


def outcome_for_status(status_code: int) -> str:
    if 200 <= status_code < 400:
        return OUTCOME_SUCCESS
    if status_code in (400, 405, 409, 422):
        return OUTCOME_INVALID
    if status_code in (401, 403):
        return OUTCOME_DENIED
    return OUTCOME_ERROR


def record_from_request(request, response) -> None:
    """Emit adoption telemetry for known enterprise endpoints.

    Called from the shared ``after_request`` hook so success, invalid-input,
    permission-denied, and error paths are all counted exactly once.
    """
    route = request.url_rule.rule if request.url_rule else ""
    outcome = outcome_for_status(response.status_code)
    if route == "/auth/api/domains/challenge":
        record_sso_event(ACTION_CHALLENGE, outcome)
    elif route == "/auth/api/domains/verify":
        record_sso_event(ACTION_VERIFY, outcome)
    elif route == "/auth/api/domains/<int:domain_id>":
        record_sso_event(ACTION_ENFORCE, outcome)
    elif route == "/admin/audit/export":
        export_format = (request.args.get("format") or FORMAT_JSON).strip().lower()
        record_audit_export(export_format, outcome)
    elif route == "/admin/organizations/<int:workspace_id>/roles":
        if request.method == "POST":
            record_custom_role_event(ACTION_CREATE, outcome)
    elif route == "/admin/organizations/<int:workspace_id>/roles/<int:role_id>":
        if request.method == "PATCH":
            record_custom_role_event(ACTION_UPDATE, outcome)
        elif request.method == "DELETE":
            record_custom_role_event(ACTION_DELETE, outcome)
    elif route == "/admin/organizations/<int:workspace_id>/scim-tokens":
        if request.method == "POST":
            record_scim_event(ACTION_CREATE, outcome)
    elif route == "/admin/organizations/<int:workspace_id>/scim-tokens/<int:token_id>":
        if request.method == "PATCH":
            record_scim_event(ACTION_UPDATE, outcome)
        elif request.method == "DELETE":
            record_scim_event(ACTION_REVOKE, outcome)


__all__ = [
    "ACTION_ASSIGN",
    "ACTION_CHALLENGE",
    "ACTION_CREATE",
    "ACTION_DELETE",
    "ACTION_DEPROVISION",
    "ACTION_DISABLE",
    "ACTION_ENFORCE",
    "ACTION_EXPORT",
    "ACTION_LOGIN",
    "ACTION_LOGOUT",
    "ACTION_PROVISION",
    "ACTION_REVOKE",
    "ACTION_SYNC",
    "ACTION_UPDATE",
    "ACTION_USE",
    "ACTION_VERIFY",
    "AUDIT_EXPORT_EVENTS",
    "CUSTOM_ROLE_EVENTS",
    "ENTERPRISE_FEATURE_EVENTS",
    "EXPORT_FORMATS",
    "FEATURES",
    "FEATURE_AUDIT_EXPORT",
    "FEATURE_CUSTOM_ROLES",
    "FEATURE_SCIM",
    "FEATURE_SSO",
    "FORMAT_CSV",
    "FORMAT_JSON",
    "OUTCOMES",
    "OUTCOME_DENIED",
    "OUTCOME_ERROR",
    "OUTCOME_INVALID",
    "OUTCOME_SUCCESS",
    "SCIM_EVENTS",
    "SSO_EVENTS",
    "outcome_for_status",
    "record_audit_export",
    "record_custom_role_event",
    "record_enterprise_event",
    "record_from_request",
    "record_scim_event",
    "record_sso_event",
]
