"""Integration health snapshots and manual connection tests (#684).

The stored row exposes the last successful call and the last sanitized error for
one workspace integration. Recording is best-effort and never raises into a
caller that is already delivering a notification.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db
from app.models.integration_health import (
    INTEGRATIONS,
    STATUS_ERROR,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    IntegrationHealth,
)
from app.services.integrations.common import (
    IntegrationError,
    IntegrationNotFound,
    IntegrationValidationError,
    credential_names,
    is_configured,
    require_owner,
    sanitize_error,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _row(workspace_id: int, integration: str, *, create: bool = False):
    row = IntegrationHealth.query.filter_by(
        workspace_id=workspace_id,
        integration=integration,
    ).first()
    if row is None and create:
        row = IntegrationHealth(
            workspace_id=workspace_id,
            integration=integration,
            status=STATUS_UNKNOWN,
        )
        db.session.add(row)
        db.session.flush()
    return row


def record_success(workspace_id: int, integration: str, *, status_code=None) -> None:
    """Record a successful call while retaining any prior last error as history."""
    try:
        row = _row(workspace_id, integration, create=True)
        row.status = STATUS_HEALTHY
        row.last_success_at = _now()
        row.last_checked_at = row.last_success_at
        if status_code is not None:
            row.last_status_code = int(status_code)
        db.session.commit()
    except Exception:  # pragma: no cover - health must never break delivery
        db.session.rollback()


def record_error(
    workspace_id: int,
    integration: str,
    error,
    *,
    code: str | None = None,
    status_code=None,
) -> None:
    """Record a bounded, sanitized failure for operator visibility."""
    try:
        row = _row(workspace_id, integration, create=True)
        row.status = STATUS_ERROR
        row.last_error_at = _now()
        row.last_checked_at = row.last_error_at
        row.last_error = sanitize_error(error)
        row.last_error_code = (code or getattr(error, "code", None) or "error")[:50]
        if status_code is not None:
            row.last_status_code = int(status_code)
        db.session.commit()
    except Exception:  # pragma: no cover - health must never break delivery
        db.session.rollback()


def get_health(workspace_id: int, integration: str) -> dict:
    """Return one integration's health snapshot (or an unknown placeholder)."""
    row = _row(workspace_id, integration)
    if row is None:
        return {
            "integration": integration,
            "status": STATUS_UNKNOWN,
            "last_success_at": None,
            "last_error_at": None,
            "last_error": None,
            "last_error_code": None,
            "last_status_code": None,
            "last_checked_at": None,
        }
    return row.to_dict()


def list_health(workspace_id: int) -> list[dict]:
    """Return health snapshots for every supported integration."""
    return [get_health(workspace_id, integration) for integration in INTEGRATIONS]


def list_integrations(workspace_id: int) -> list[dict]:
    """Return configuration metadata and health for every integration."""
    entries = []
    for integration in INTEGRATIONS:
        names = credential_names(workspace_id, integration)
        entries.append(
            {
                "integration": integration,
                "configured": is_configured(workspace_id, integration),
                "credential_names": sorted(names),
                "supports_test": True,
                "health": get_health(workspace_id, integration),
            }
        )
    return entries


def _normalize_integration(value) -> str:
    integration = str(value or "").strip().lower()
    if integration not in INTEGRATIONS:
        raise IntegrationValidationError(f"unsupported integration: {integration or '<empty>'}")
    return integration


def test_connection(workspace, user, integration) -> dict:
    """Run a manual connection/test-send for one integration.

    Owner-only. Returns a structured result instead of raising so the API can
    report a failure without leaking a traceback or a credential.
    """
    require_owner(workspace.id, user)
    integration = _normalize_integration(integration)
    try:
        if integration == "slack":
            from app.services.integrations import slack

            return slack.test_connection(workspace)
        if integration == "teams":
            from app.services.integrations import teams

            return teams.test_connection(workspace)
        from app.services.integrations import email

        return email.test_connection(workspace, user)
    except IntegrationError as exc:
        return {
            "status": "error",
            "integration": integration,
            "code": getattr(exc, "code", "integration_error"),
            "error": str(exc),
        }
    except Exception as exc:
        record_error(workspace.id, integration, exc)
        return {
            "status": "error",
            "integration": integration,
            "code": "integration_error",
            "error": sanitize_error(exc),
        }


def get_integration(workspace_id: int, integration: str) -> dict:
    """Return one integration's configuration + health, or raise not-found."""
    integration = _normalize_integration(integration)
    from app.models import Workspace

    if db.session.get(Workspace, workspace_id) is None:
        raise IntegrationNotFound("Workspace not found.")
    names = credential_names(workspace_id, integration)
    return {
        "integration": integration,
        "configured": is_configured(workspace_id, integration),
        "credential_names": sorted(names),
        "supports_test": True,
        "health": get_health(workspace_id, integration),
    }


__all__ = [
    "get_health",
    "get_integration",
    "list_health",
    "list_integrations",
    "record_error",
    "record_success",
    "test_connection",
]
