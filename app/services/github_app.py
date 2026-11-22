"""GitHub App installation lifecycle (#676).

The existing OAuth flow stays untouched: this module only manages GitHub App
installations, which are identified by an installation id and carry their own
repository selection. Installation tokens are encrypted at rest and never
serialized. Updates and revocations are handled both by owner-scoped API calls
and by signed GitHub webhook events.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from flask import current_app

from app.extensions import db
from app.models import Workspace
from app.models.github_app_installation import (
    REPOSITORY_SELECTION_ALL,
    REPOSITORY_SELECTION_SELECTED,
    REPOSITORY_SELECTIONS,
    STATUS_ACTIVE,
    STATUS_REVOKED,
    STATUS_SUSPENDED,
    GitHubAppInstallation,
)
from app.services.permissions import can

WEBHOOK_SIGNATURE_HEADER = "X-Hub-Signature-256"
MAX_REPOSITORY_IDS = 5000


class GitHubAppError(ValueError):
    """Base class for structured GitHub App errors."""

    code = "github_app_error"
    http_status = 400


class GitHubAppValidationError(GitHubAppError):
    code = "invalid_input"


class GitHubAppPermissionError(PermissionError):
    code = "permission_denied"
    http_status = 403


class GitHubAppNotFound(GitHubAppError):
    code = "not_found"
    http_status = 404


class GitHubAppConflict(GitHubAppError):
    code = "installation_conflict"
    http_status = 409


class GitHubAppNotConfigured(GitHubAppError):
    code = "not_configured"
    http_status = 409


def _require_owner(workspace_id: int, user) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise GitHubAppNotFound("Workspace not found.")
    if not can("manage_integrations", workspace_id, user):
        raise GitHubAppPermissionError(
            "You do not have permission to manage GitHub App installations."
        )
    return workspace


def _positive_int(value, label: str) -> int:
    if isinstance(value, bool):
        raise GitHubAppValidationError(f"{label} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise GitHubAppValidationError(f"{label} must be a positive integer") from exc
    if number <= 0:
        raise GitHubAppValidationError(f"{label} must be a positive integer")
    return number


def _repository_selection(value) -> str:
    selection = str(value or REPOSITORY_SELECTION_SELECTED).strip().lower()
    if selection not in REPOSITORY_SELECTIONS:
        raise GitHubAppValidationError("repository_selection must be 'all' or 'selected'")
    return selection


def _repository_ids(values) -> list[int]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        raise GitHubAppValidationError("repository_ids must be a list")
    if len(values) > MAX_REPOSITORY_IDS:
        raise GitHubAppValidationError(
            f"repository_ids must contain at most {MAX_REPOSITORY_IDS} entries"
        )
    normalized: list[int] = []
    for value in values:
        repository_id = _positive_int(value, "repository id")
        if repository_id not in normalized:
            normalized.append(repository_id)
    return normalized


def get_installation(workspace_id: int, installation_id: int) -> GitHubAppInstallation:
    """Return a workspace's installation by GitHub installation id, or 404."""
    row = GitHubAppInstallation.query.filter_by(
        workspace_id=workspace_id,
        installation_id=_positive_int(installation_id, "installation_id"),
    ).first()
    if row is None:
        raise GitHubAppNotFound("GitHub App installation not found.")
    return row


def list_installations(workspace_id: int, *, user) -> list[GitHubAppInstallation]:
    """Return every installation for a workspace, newest first."""
    _require_owner(workspace_id, user)
    return (
        GitHubAppInstallation.query.filter_by(workspace_id=workspace_id)
        .order_by(GitHubAppInstallation.created_at.desc(), GitHubAppInstallation.id.desc())
        .all()
    )


def record_installation(
    workspace_id: int,
    user,
    installation_id,
    account_login,
    *,
    account_id=None,
    account_type="User",
    repository_selection=REPOSITORY_SELECTION_SELECTED,
    repository_ids=None,
    access_token=None,
    token_expires_at=None,
) -> GitHubAppInstallation:
    """Create or update an installation owned by ``user``'s workspace."""
    _require_owner(workspace_id, user)
    installation_id = _positive_int(installation_id, "installation_id")
    login = str(account_login or "").strip()
    if not login or len(login) > 200:
        raise GitHubAppValidationError("account_login must be 1-200 characters")
    selection = _repository_selection(repository_selection)
    repositories = _repository_ids(repository_ids)
    if selection == REPOSITORY_SELECTION_ALL:
        # ``all`` is authoritative; keeping a stale id list would misrepresent
        # the installation's access.
        repositories = []

    existing = GitHubAppInstallation.query.filter_by(installation_id=installation_id).first()
    if existing is not None and existing.workspace_id != workspace_id:
        raise GitHubAppConflict("This installation belongs to another workspace.")
    row = existing or GitHubAppInstallation(
        workspace_id=workspace_id,
        user_id=getattr(user, "id", None),
        installation_id=installation_id,
    )
    row.workspace_id = workspace_id
    row.user_id = getattr(user, "id", row.user_id)
    row.account_login = login
    row.account_id = _positive_int(account_id, "account_id") if account_id is not None else None
    row.account_type = str(account_type or "User")[:50]
    row.repository_selection = selection
    row.set_repository_ids(repositories)
    row.status = STATUS_ACTIVE
    row.suspended_at = None
    row.revoked_at = None
    if access_token:
        row.set_access_token(str(access_token))
        row.token_expires_at = token_expires_at
    if existing is None:
        db.session.add(row)
    db.session.commit()
    return row


def update_installation(
    workspace_id: int, user, installation_id, **changes
) -> GitHubAppInstallation:
    """Update repository selection and/or metadata for an existing installation."""
    row = get_installation(workspace_id, installation_id)
    _require_owner(workspace_id, user)
    if row.status == STATUS_REVOKED:
        raise GitHubAppConflict("Revoked installations cannot be updated.")
    if "account_login" in changes:
        login = str(changes.get("account_login") or "").strip()
        if not login or len(login) > 200:
            raise GitHubAppValidationError("account_login must be 1-200 characters")
        row.account_login = login
    if "account_type" in changes:
        row.account_type = str(changes.get("account_type") or "User")[:50]
    if "account_id" in changes and changes.get("account_id") is not None:
        row.account_id = _positive_int(changes["account_id"], "account_id")
    if "repository_selection" in changes:
        row.repository_selection = _repository_selection(changes["repository_selection"])
    if "repository_ids" in changes:
        row.set_repository_ids(_repository_ids(changes["repository_ids"]))
    if row.repository_selection == REPOSITORY_SELECTION_ALL:
        row.set_repository_ids([])
    if changes.get("access_token"):
        row.set_access_token(str(changes["access_token"]))
        row.token_expires_at = changes.get("token_expires_at")
    db.session.commit()
    return row


def revoke_installation(workspace_id: int, user, installation_id) -> GitHubAppInstallation:
    """Revoke an installation and clear its encrypted token."""
    row = get_installation(workspace_id, installation_id)
    _require_owner(workspace_id, user)
    _mark_revoked(row)
    return row


def _mark_revoked(row: GitHubAppInstallation, *, now: datetime | None = None) -> None:
    row.status = STATUS_REVOKED
    row.revoked_at = now or datetime.now(UTC)
    row.access_token_encrypted = None
    row.token_expires_at = None
    db.session.commit()


def _mark_suspended(row: GitHubAppInstallation, *, now: datetime | None = None) -> None:
    row.status = STATUS_SUSPENDED
    row.suspended_at = now or datetime.now(UTC)
    row.access_token_encrypted = None
    row.token_expires_at = None
    db.session.commit()


def _mark_active(row: GitHubAppInstallation) -> None:
    row.status = STATUS_ACTIVE
    row.suspended_at = None
    row.revoked_at = None
    db.session.commit()


def _apply_installation_payload(row: GitHubAppInstallation, installation: dict) -> None:
    """Copy non-secret installation metadata from a GitHub webhook payload."""
    if installation.get("account"):
        account = installation.get("account") or {}
        if account.get("login"):
            row.account_login = str(account["login"])[:200]
        if account.get("id") is not None:
            row.account_id = _positive_int(account["id"], "account_id")
        if account.get("type"):
            row.account_type = str(account["type"])[:50]
    if installation.get("repository_selection"):
        row.repository_selection = _repository_selection(installation["repository_selection"])
    if installation.get("id") is not None and not row.installation_id:
        row.installation_id = _positive_int(installation["id"], "installation_id")


def verify_webhook_signature(body: bytes, signature: str, *, secret: str | None = None) -> bool:
    """Verify a GitHub ``X-Hub-Signature-256`` over the raw request body."""
    configured = secret or current_app.config.get("GITHUB_APP_WEBHOOK_SECRET", "")
    if not configured:
        raise GitHubAppNotConfigured("GITHUB_APP_WEBHOOK_SECRET is not configured.")
    expected = (
        "sha256=" + hmac.new(configured.encode("utf-8"), body or b"", hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, str(signature or ""))


def handle_webhook_event(event: str, payload: dict) -> dict:
    """Apply an installation webhook to the matching stored installation.

    Unknown installations return ``handled: False`` so a delivery for another
    tenant is acknowledged without creating workspace data out of thin air.
    """
    if not isinstance(payload, dict):
        raise GitHubAppValidationError("webhook payload must be a JSON object")
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        raise GitHubAppValidationError("webhook payload has no installation")
    installation_id = _positive_int(installation.get("id"), "installation id")
    row = GitHubAppInstallation.query.filter_by(installation_id=installation_id).first()
    if row is None:
        return {
            "handled": False,
            "reason": "unknown_installation",
            "installation_id": installation_id,
        }
    if row.status == STATUS_REVOKED and event != "installation":
        return {"handled": False, "reason": "revoked", "installation": row.to_dict()}

    action = str(payload.get("action") or "").lower()
    _apply_installation_payload(row, installation)
    if event == "installation" and action == "deleted":
        _mark_revoked(row)
    elif event == "installation" and action == "suspend":
        _mark_suspended(row)
    elif event == "installation" and action == "unsuspend":
        _mark_active(row)
    elif event in ("installation", "installation_repositories"):
        if "repositories" in payload or "repositories_added" in payload:
            repositories = payload.get("repositories") or (
                list(payload.get("repositories_added") or [])
                + list(payload.get("repositories_removed") or [])
            )
            ids = [repo.get("id") for repo in repositories if isinstance(repo, dict)]
            if "repositories_removed" in payload and "repositories" not in payload:
                remaining = set(row.repository_ids()) - set(_repository_ids(ids))
                row.set_repository_ids(sorted(remaining))
            else:
                row.set_repository_ids(_repository_ids(ids))
        row.status = STATUS_ACTIVE
        row.suspended_at = None
        db.session.commit()
    else:
        db.session.commit()

    return {
        "handled": True,
        "action": action,
        "event": event,
        "installation": row.to_dict(),
    }


__all__ = [
    "WEBHOOK_SIGNATURE_HEADER",
    "GitHubAppConflict",
    "GitHubAppError",
    "GitHubAppNotConfigured",
    "GitHubAppNotFound",
    "GitHubAppPermissionError",
    "GitHubAppValidationError",
    "get_installation",
    "handle_webhook_event",
    "list_installations",
    "record_installation",
    "revoke_installation",
    "update_installation",
    "verify_webhook_signature",
]
