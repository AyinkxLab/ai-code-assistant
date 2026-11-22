"""Create Jira issues from AI review findings (#680).

Jira credentials are stored encrypted through the existing
``integration_credentials`` service (``integration="jira"``). Severity is mapped
to a Jira priority by a fixed policy. Every created issue is recorded in
``jira_issue_links``; the stored external key is the durable duplicate guard, so
exporting the same finding twice reuses the first issue and makes no second API
call.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import requests
from flask import current_app

from app.extensions import db
from app.models import JiraIssueLink, ReviewFinding, Workspace
from app.services.integration_credentials import (
    IntegrationCredentialError,
    set_credential,
)
from app.services.integrations.common import sanitize_error
from app.services.permissions import can

JIRA_INTEGRATION = "jira"
CREDENTIAL_NAMES = ("base_url", "email", "api_token", "project_key")

SEVERITY_PRIORITY = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "informational": "Lowest",
}

_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}$")
_MAX_SUMMARY = 255


class JiraError(ValueError):
    """Base class for structured Jira export errors."""

    code = "jira_error"
    http_status = 400


class JiraValidationError(JiraError):
    code = "invalid_input"


class JiraNotConfigured(JiraError):
    code = "not_configured"
    http_status = 409


class JiraNotFound(JiraError):
    code = "not_found"
    http_status = 404


class JiraPermissionError(PermissionError):
    code = "permission_denied"
    http_status = 403


class JiraDeliveryError(JiraError):
    code = "delivery_failed"
    http_status = 502


class JiraDuplicateError(JiraError):
    code = "duplicate_issue"
    http_status = 409

    def __init__(self, message: str, *, external_key: str) -> None:
        super().__init__(message)
        self.external_key = external_key


def severity_to_priority(severity) -> str:
    """Map a review finding severity to a Jira priority name.

    Unknown values are rejected rather than silently defaulted, so a corrupt
    finding cannot create an issue at an unexpected priority.
    """
    key = str(severity or "").strip().lower()
    try:
        return SEVERITY_PRIORITY[key]
    except KeyError as exc:
        raise JiraValidationError(f"unknown finding severity: {key or '<empty>'}") from exc


def _require_owner(workspace_id: int, user) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise JiraNotFound("Workspace not found.")
    if not can("manage_integrations", workspace_id, user):
        raise JiraPermissionError("You do not have permission to export review findings.")
    return workspace


def _config(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _credential(workspace_id: int, name: str) -> str:
    from app.services.integrations.common import load_credential_value

    try:
        return load_credential_value(workspace_id, JIRA_INTEGRATION, name)
    except Exception as exc:
        raise JiraNotConfigured(f"Jira {name} is not configured for this workspace.") from exc


def configure_jira(
    workspace_id: int,
    user,
    *,
    base_url,
    email,
    api_token,
    project_key,
) -> dict:
    """Validate and store an encrypted Jira configuration (owner only)."""
    _require_owner(workspace_id, user)
    parsed = urlparse(str(base_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise JiraValidationError("base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise JiraValidationError("base_url must not contain credentials")
    email = str(email or "").strip()
    if not email or "@" not in email:
        raise JiraValidationError("email must be a valid address")
    api_token = str(api_token or "").strip()
    if not api_token:
        raise JiraValidationError("api_token is required")
    project_key = str(project_key or "").strip()
    if not _PROJECT_KEY_RE.match(project_key):
        raise JiraValidationError("project_key must be 1-50 letters, digits, or underscores")
    try:
        for name, value in (
            ("base_url", parsed.geturl()),
            ("email", email),
            ("api_token", api_token),
            ("project_key", project_key),
        ):
            set_credential(workspace_id, JIRA_INTEGRATION, name, value, user=user)
    except IntegrationCredentialError as exc:
        raise JiraValidationError(str(exc)) from exc
    return {
        "integration": JIRA_INTEGRATION,
        "configured": True,
        "project_key": project_key,
        "credential_names": list(CREDENTIAL_NAMES),
    }


def is_configured(workspace_id: int) -> bool:
    from app.services.integrations.common import credential_names

    return set(CREDENTIAL_NAMES).issubset(credential_names(workspace_id, JIRA_INTEGRATION))


def _review_for_finding(finding: ReviewFinding):
    review = getattr(finding, "review", None)
    if review is None and finding.review_id:
        from app.models import Review

        review = db.session.get(Review, finding.review_id)
    if review is None:
        raise JiraValidationError("finding does not belong to a review")
    return review


def _require_finding_scope(workspace_id: int, finding: ReviewFinding, user) -> None:
    _require_owner(workspace_id, user)
    if not isinstance(finding, ReviewFinding) or finding.id is None:
        raise JiraValidationError("a persisted review finding is required")
    review = _review_for_finding(finding)
    if review.user_id == getattr(user, "id", None):
        return
    project = getattr(review, "project", None)
    if project is not None and project.workspace_id == workspace_id:
        return
    raise JiraPermissionError("This review finding does not belong to your workspace.")


def _issue_fields(workspace_id: int, finding: ReviewFinding, project_key: str | None) -> dict:
    priority = severity_to_priority(finding.severity)
    key = (project_key or _credential(workspace_id, "project_key")).strip()
    if not _PROJECT_KEY_RE.match(key):
        raise JiraValidationError("project_key must be 1-50 letters, digits, or underscores")
    location = finding.file or "review"
    if finding.line:
        location = f"{location}:{finding.line}"
    summary = f"[{finding.severity}] {finding.category}: {location}"
    if len(summary) > _MAX_SUMMARY:
        summary = summary[: _MAX_SUMMARY - 3] + "..."
    description_parts = [finding.explanation or ""]
    if finding.recommendation:
        description_parts += ["", "Recommendation:", finding.recommendation]
    description_parts += [
        "",
        f"Confidence: {finding.confidence}",
        f"Finding: {finding.id}",
        f"Review: {finding.review_id}",
    ]
    labels = ["ai-review", str(finding.severity), str(finding.category)]
    return {
        "project": {"key": key},
        "summary": summary,
        "description": "\n".join(description_parts),
        "issuetype": {"name": "Bug"},
        "priority": {"name": priority},
        "labels": labels[:10],
    }


def _existing_link(workspace_id: int, finding_id: int) -> JiraIssueLink | None:
    return JiraIssueLink.query.filter_by(
        workspace_id=workspace_id,
        finding_id=finding_id,
    ).first()


def create_issue_for_finding(
    workspace_id: int,
    finding: ReviewFinding,
    *,
    user,
    project_key: str | None = None,
    post=None,
    now=None,
) -> JiraIssueLink:
    """Create (or return) the Jira issue linked to ``finding``.

    Idempotent: a second call for the same ``(workspace_id, finding_id)`` returns
    the stored link without contacting Jira, so retries cannot create
    duplicates.
    """
    link, _created = create_or_get_issue_for_finding(
        workspace_id,
        finding,
        user=user,
        project_key=project_key,
        post=post,
        now=now,
    )
    return link


def create_or_get_issue_for_finding(
    workspace_id: int,
    finding: ReviewFinding,
    *,
    user,
    project_key: str | None = None,
    post=None,
    now=None,
) -> tuple[JiraIssueLink, bool]:
    """Return ``(link, created)`` for the finding's Jira issue."""
    _require_finding_scope(workspace_id, finding, user)
    existing = _existing_link(workspace_id, finding.id)
    if existing is not None:
        return existing, False

    priority = severity_to_priority(finding.severity)
    fields = _issue_fields(workspace_id, finding, project_key)
    base_url = _credential(workspace_id, "base_url").rstrip("/")
    email = _credential(workspace_id, "email")
    token = _credential(workspace_id, "api_token")
    poster = post or requests.post
    timeout = float(_config("INTEGRATION_REQUEST_TIMEOUT", 10))
    try:
        response = poster(
            f"{base_url}/rest/api/3/issue",
            json={"fields": fields},
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        raise JiraDeliveryError(sanitize_error(exc)) from exc

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise JiraDeliveryError(f"Jira returned HTTP {status_code}")

    try:
        data = response.json()
    except (TypeError, ValueError) as exc:
        raise JiraDeliveryError("Jira returned a non-JSON response") from exc
    if not isinstance(data, dict) or not data.get("key"):
        raise JiraDeliveryError("Jira response did not include an issue key")
    external_key = str(data["key"])[:100]
    external_id = str(data.get("id"))[:100] if data.get("id") is not None else None
    key = fields["project"]["key"]

    link = JiraIssueLink(
        workspace_id=workspace_id,
        review_id=finding.review_id,
        finding_id=finding.id,
        project_key=key,
        external_key=external_key,
        external_id=external_id,
        severity=finding.severity,
        priority=priority,
        created_by_id=getattr(user, "id", None),
    )
    if now is not None:
        link.created_at = now
    db.session.add(link)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # A concurrent export may have stored the same external key or the
        # same finding; return the durable row instead of creating a duplicate.
        existing = _existing_link(workspace_id, finding.id)
        if existing is not None:
            return existing, False
        raise
    return link, True


__all__ = [
    "CREDENTIAL_NAMES",
    "JIRA_INTEGRATION",
    "SEVERITY_PRIORITY",
    "JiraDeliveryError",
    "JiraDuplicateError",
    "JiraError",
    "JiraNotConfigured",
    "JiraNotFound",
    "JiraPermissionError",
    "JiraValidationError",
    "configure_jira",
    "create_issue_for_finding",
    "create_or_get_issue_for_finding",
    "is_configured",
    "severity_to_priority",
]
