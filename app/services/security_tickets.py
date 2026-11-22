"""Create Jira or Linear tickets when a high-risk finding is acknowledged (#630).

The workspace opts in by storing encrypted credentials through
:mod:`app.services.integration_credentials` (``integration="ticketing"``). When
a finding with severity ``critical`` or ``high`` transitions to
``addressed=True``, :func:`create_for_acknowledged_finding` creates a ticket in
the configured provider.

Safety properties:

* The request payload is built from known finding fields, every string is
  length-bounded, and the encoded JSON is capped at :data:`MAX_REQUEST_BYTES`.
* A durable :class:`~app.models.security_ticket.SecurityTicketLink` row keyed by
  ``(workspace_id, finding_id)`` makes retries idempotent: the second call
  returns the stored link without contacting the provider.
* Misses (not acknowledged, not high risk, no workspace, not configured) are
  no-ops; real failures raise a typed :class:`TicketingError` with a stable
  code and HTTP status.
* Callers can inject ``post`` so tests use a mocked provider, never the network.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

import requests
from flask import current_app

from app.extensions import db
from app.models import Review, ReviewFinding, SecurityTicketLink
from app.services.integration_credentials import (
    IntegrationCredentialError,
    set_credential,
)
from app.services.integrations.common import (
    IntegrationNotConfigured,
    credential_names,
    load_credential_value,
    sanitize_error,
)

INTEGRATION = "ticketing"
PROVIDERS = ("jira", "linear")
HIGH_RISK_SEVERITIES = frozenset({"critical", "high"})

REQUIRED_CREDENTIALS = {
    "jira": ("base_url", "email", "api_token", "project_key"),
    "linear": ("api_token", "team_id"),
}

MAX_TITLE = 200
MAX_DESCRIPTION = 4000
MAX_REQUEST_BYTES = 16_384

LINEAR_ENDPOINT = "https://api.linear.app/graphql"
LINEAR_MUTATION = (
    "mutation IssueCreate($input: IssueCreateInput!) {"
    " issueCreate(input: $input) { success issue { identifier url } } }"
)

_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}$")

JIRA_PRIORITY = {"critical": "Highest", "high": "High"}
LINEAR_PRIORITY = {"critical": 1, "high": 2}


class TicketingError(ValueError):
    """Base class for structured ticketing errors."""

    code = "ticketing_error"
    http_status = 400


class TicketingValidationError(TicketingError):
    code = "invalid_input"


class TicketingPermissionError(TicketingError, PermissionError):
    code = "permission_denied"
    http_status = 403


class TicketingNotConfigured(TicketingError):
    code = "not_configured"
    http_status = 409


class TicketingNotFound(TicketingError):
    code = "not_found"
    http_status = 404


class TicketingDeliveryError(TicketingError):
    code = "delivery_failed"
    http_status = 502


def _config(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:  # no application context
        return default


def _normalize_provider(provider) -> str:
    value = str(provider or "").strip().lower()
    if value not in PROVIDERS:
        raise TicketingValidationError(
            "provider must be 'jira' or 'linear'.",
        )
    return value


def _nonempty(value, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise TicketingValidationError(f"{label} is required.")
    if len(text) > limit:
        raise TicketingValidationError(f"{label} must be {limit} characters or fewer.")
    return text


def _jira_base_url(value) -> str:
    text = _nonempty(value, "base_url", 500)
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise TicketingValidationError("base_url must be an http(s) URL.")
    if parsed.username or parsed.password:
        raise TicketingValidationError("base_url must not contain credentials.")
    return parsed.geturl()


def configure_ticketing(
    workspace_id: int,
    user,
    *,
    provider,
    base_url=None,
    email=None,
    api_token=None,
    project_key=None,
    team_id=None,
) -> dict:
    """Validate and store encrypted ticketing credentials (owner only).

    Raises :class:`TicketingPermissionError` for a non-owner and
    :class:`TicketingValidationError` for malformed settings. Secrets are
    written through ``integration_credentials`` and never returned.
    """
    provider = _normalize_provider(provider)

    if provider == "jira":
        base_url = _jira_base_url(base_url)
        email = _nonempty(email, "email", 255)
        if "@" not in email:
            raise TicketingValidationError("email must be a valid address.")
        api_token = _nonempty(api_token, "api_token", 500)
        project_key = _nonempty(project_key, "project_key", 50)
        if not _PROJECT_KEY_RE.match(project_key):
            raise TicketingValidationError(
                "project_key must be 1-50 letters, digits, or underscores."
            )
        settings = {
            "base_url": base_url,
            "email": email,
            "api_token": api_token,
            "project_key": project_key,
        }
    else:
        api_token = _nonempty(api_token, "api_token", 500)
        team_id = _nonempty(team_id, "team_id", 100)
        settings = {"api_token": api_token, "team_id": team_id}

    try:
        set_credential(workspace_id, INTEGRATION, "provider", provider, user=user)
        for name, value in settings.items():
            set_credential(workspace_id, INTEGRATION, name, value, user=user)
    except PermissionError as exc:
        raise TicketingPermissionError(str(exc)) from exc
    except IntegrationCredentialError as exc:
        raise TicketingValidationError(str(exc)) from exc

    return {
        "integration": INTEGRATION,
        "provider": provider,
        "configured": True,
        "credential_names": list(REQUIRED_CREDENTIALS[provider]),
    }


def configured_provider(workspace_id: int) -> str | None:
    """Return the configured provider, or ``None`` when the workspace opted out."""
    try:
        provider = load_credential_value(workspace_id, INTEGRATION, "provider")
    except IntegrationNotConfigured:
        return None
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDERS:
        return None
    if not set(REQUIRED_CREDENTIALS[provider]).issubset(
        credential_names(workspace_id, INTEGRATION)
    ):
        return None
    return provider


def is_configured(workspace_id: int) -> bool:
    """Return ``True`` when this workspace has a complete ticketing setup."""
    return configured_provider(workspace_id) is not None


def _bounded(value, limit: int, *, single_line: bool = False) -> str:
    text = str(value or "")
    if single_line:
        text = " ".join(text.split())
    return text[:limit]


def _idempotency_key(workspace_id: int, finding_id: int) -> str:
    raw = f"ticketing:{workspace_id}:{finding_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _workspace_for_finding(finding: ReviewFinding) -> tuple[int | None, Review | None]:
    if not isinstance(finding, ReviewFinding) or finding.id is None:
        raise TicketingValidationError("a persisted review finding is required.")
    review = finding.review
    if review is None and finding.review_id:
        review = db.session.get(Review, finding.review_id)
    if review is None:
        raise TicketingValidationError("finding does not belong to a review.")
    project = review.project
    if project is None or project.workspace_id is None:
        return None, review
    return project.workspace_id, review


def _description(finding: ReviewFinding, workspace_id: int) -> str:
    location = finding.file or "review"
    if finding.line:
        location = f"{location}:{finding.line}"
    parts = [
        f"High-risk {finding.severity} finding in {location}.",
        "",
        _bounded(finding.explanation, MAX_DESCRIPTION),
    ]
    if finding.recommendation:
        parts += ["", "Recommendation:", _bounded(finding.recommendation, 1000)]
    parts += [
        "",
        f"Category: {finding.category}",
        f"Confidence: {finding.confidence}",
        f"Finding: {finding.id}",
        f"Review: {finding.review_id}",
        f"Idempotency key: {_idempotency_key(workspace_id, finding.id)}",
    ]
    return _bounded("\n".join(parts), MAX_DESCRIPTION)


def _build_payload(provider: str, workspace_id: int, finding: ReviewFinding) -> dict:
    location = finding.file or "review"
    if finding.line:
        location = f"{location}:{finding.line}"
    title = _bounded(
        f"[{finding.severity.upper()}] {finding.category}: {location}",
        MAX_TITLE,
        single_line=True,
    )
    description = _description(finding, workspace_id)
    severity = str(finding.severity).lower()

    if provider == "jira":
        project_key = load_credential_value(workspace_id, INTEGRATION, "project_key")
        return {
            "fields": {
                "project": {"key": project_key},
                "summary": title,
                "description": description,
                "issuetype": {"name": "Bug"},
                "priority": {"name": JIRA_PRIORITY[severity]},
                "labels": ["security", "ai-review", severity],
            }
        }
    return {
        "query": LINEAR_MUTATION,
        "variables": {
            "input": {
                "teamId": load_credential_value(workspace_id, INTEGRATION, "team_id"),
                "title": title,
                "description": description,
                "priority": LINEAR_PRIORITY[severity],
            }
        },
    }


def _response_json(response, provider: str) -> dict:
    try:
        data = response.json()
    except (TypeError, ValueError) as exc:
        raise TicketingDeliveryError(f"{provider} returned a non-JSON response.") from exc
    if not isinstance(data, dict):
        raise TicketingDeliveryError(f"{provider} returned an unexpected response.")
    return data


def _deliver(
    provider: str,
    workspace_id: int,
    payload: dict,
    post,
) -> tuple[str, str | None]:
    """Send one bounded request and return ``(external_key, external_url)``."""
    timeout = float(_config("INTEGRATION_REQUEST_TIMEOUT", 10))
    try:
        if provider == "jira":
            base_url = load_credential_value(workspace_id, INTEGRATION, "base_url").rstrip("/")
            email = load_credential_value(workspace_id, INTEGRATION, "email")
            token = load_credential_value(workspace_id, INTEGRATION, "api_token")
            response = post(
                f"{base_url}/rest/api/3/issue",
                json=payload,
                auth=(email, token),
                headers={"Accept": "application/json"},
                timeout=timeout,
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if not 200 <= status < 300:
                raise TicketingDeliveryError(f"Jira returned HTTP {status}.")
            data = _response_json(response, "Jira")
            key = str(data.get("key") or "").strip()
            if not key:
                raise TicketingDeliveryError("Jira response did not include an issue key.")
            return key[:100], f"{base_url}/browse/{key}"

        token = load_credential_value(workspace_id, INTEGRATION, "api_token")
        response = post(
            LINEAR_ENDPOINT,
            json=payload,
            headers={"Accept": "application/json", "Authorization": token},
            timeout=timeout,
            allow_redirects=False,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise TicketingDeliveryError(f"Linear returned HTTP {status}.")
        data = _response_json(response, "Linear")
        result = (data.get("data") or {}).get("issueCreate") or {}
        issue = result.get("issue") or {}
        key = str(issue.get("identifier") or "").strip()
        if not key:
            raise TicketingDeliveryError("Linear response did not include an issue identifier.")
        url = str(issue.get("url") or "").strip() or None
        return key[:100], (url[:500] if url else None)
    except TicketingError:
        raise
    except Exception as exc:
        raise TicketingDeliveryError(sanitize_error(exc)) from exc


def create_for_acknowledged_finding(
    finding: ReviewFinding,
    *,
    post=None,
    now=None,
) -> SecurityTicketLink | None:
    """Create (or return) the ticket for an acknowledged high-risk finding.

    Returns ``None`` when the finding is not acknowledged, is not high risk,
    has no workspace, or the workspace has no ticketing configuration. A real
    provider/configuration failure raises :class:`TicketingError`.
    """
    if not bool(getattr(finding, "addressed", False)):
        return None
    severity = str(getattr(finding, "severity", "")).lower()
    if severity not in HIGH_RISK_SEVERITIES:
        return None

    workspace_id, _review = _workspace_for_finding(finding)
    if workspace_id is None:
        return None
    provider = configured_provider(workspace_id)
    if provider is None:
        return None

    existing = SecurityTicketLink.query.filter_by(
        workspace_id=workspace_id,
        finding_id=finding.id,
    ).first()
    if existing is not None:
        return existing

    payload = _build_payload(provider, workspace_id, finding)
    try:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TicketingValidationError("ticket payload is not serializable.") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise TicketingValidationError(f"ticket payload exceeds {MAX_REQUEST_BYTES} bytes.")

    external_key, external_url = _deliver(provider, workspace_id, payload, post or requests.post)
    link = SecurityTicketLink(
        workspace_id=workspace_id,
        finding_id=finding.id,
        provider=provider,
        external_key=external_key,
        external_url=external_url,
        severity=severity,
    )
    if now is not None:
        link.created_at = now
    db.session.add(link)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        existing = SecurityTicketLink.query.filter_by(
            workspace_id=workspace_id,
            finding_id=finding.id,
        ).first()
        if existing is not None:
            return existing
        raise TicketingDeliveryError("Could not record the external ticket link.") from None
    return link


__all__ = [
    "HIGH_RISK_SEVERITIES",
    "INTEGRATION",
    "LINEAR_ENDPOINT",
    "MAX_DESCRIPTION",
    "MAX_REQUEST_BYTES",
    "MAX_TITLE",
    "PROVIDERS",
    "REQUIRED_CREDENTIALS",
    "TicketingDeliveryError",
    "TicketingError",
    "TicketingNotConfigured",
    "TicketingNotFound",
    "TicketingPermissionError",
    "TicketingValidationError",
    "configure_ticketing",
    "configured_provider",
    "create_for_acknowledged_finding",
    "is_configured",
]
