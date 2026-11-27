"""User data deletion / anonymization workflow (#615).

The workflow mirrors :mod:`app.services.data_export`: a per-user rate limit,
owner-only access, every state change audited, and structured failures. A
request needs an explicit confirmation phrase before anything is changed.

What completion does
--------------------
``deletion`` mode removes the user's personal content rows (conversations and
their messages, prompts, provider API keys, GitHub connections, and export
requests) and then anonymizes the account profile. ``anonymization`` mode keeps
content and only anonymizes the profile.

Neither mode deletes the :class:`~app.models.User` row or any
:class:`~app.models.AuditLog` row. The account row is retained because audit
entries reference it and because deleting it would cascade-delete owned
workspaces (which would also destroy their audit chains). Instead the profile
is cleared, the account is disabled, and workspace memberships are soft-removed.
See ``docs/user-data-deletion.md`` for the exact field-by-field contract.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import (
    ApiKey,
    Conversation,
    GithubAccount,
    Prompt,
    PromptVersion,
    UserDataExport,
    UserDeletionRequest,
    WorkspaceMember,
)
from app.models.user_deletion_request import (
    MODE_ANONYMIZATION,
    MODE_DELETION,
    MODES,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CONFIRMED,
    STATUS_REQUESTED,
)
from app.services import audit, legal_hold, ratelimit
from app.services.activity import record_role_change

#: Exact phrase a user must submit to confirm a destructive request.
CONFIRMATION_PHRASE = "delete my data"

#: Requests allowed per user per window (same window as the export workflow).
REQUEST_LIMIT = 3
REQUEST_WINDOW_SECONDS = 24 * 60 * 60


class DeletionError(Exception):
    """Base class for a failed deletion workflow operation."""


class RateLimited(DeletionError):
    """The caller has requested too many deletions in the current window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many deletion requests. Please retry later.")
        self.retry_after = retry_after


class ConfirmationMismatch(DeletionError):
    """The explicit confirmation phrase is missing or wrong."""


class DeletionConflict(DeletionError):
    """The request is not in a state that allows the operation."""


class AlreadyCompleted(DeletionError):
    """The account has already completed a deletion request."""


class LegalHoldActive(DeletionError):
    """A workspace the user belongs to is preserved by an active legal hold."""


def _now(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _get_owned(request_id: int, user) -> UserDeletionRequest:
    row = db.session.get(UserDeletionRequest, request_id)
    if row is None or user is None or row.user_id != user.id:
        raise LookupError("Deletion request not found.")
    return row


def list_requests(user, *, limit: int = 20) -> list[UserDeletionRequest]:
    """Return the user's most recent deletion requests."""
    return (
        UserDeletionRequest.query.filter_by(user_id=user.id)
        .order_by(UserDeletionRequest.requested_at.desc(), UserDeletionRequest.id.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


def request_deletion(
    user,
    *,
    mode: str = MODE_DELETION,
    now: datetime | None = None,
    enforce_rate_limit: bool = True,
    commit: bool = True,
) -> UserDeletionRequest:
    """Create a ``requested`` deletion row for ``user``."""
    if user is None or getattr(user, "id", None) is None:
        raise ValueError("A user is required to request a deletion.")
    normalized = (mode or "").strip().lower()
    if normalized not in MODES:
        raise ValueError("mode must be deletion or anonymization.")
    moment = _now(now)

    if getattr(user, "is_active", True) is False:
        raise AlreadyCompleted("This account has already been deleted or anonymized.")
    if UserDeletionRequest.query.filter(
        UserDeletionRequest.user_id == user.id,
        UserDeletionRequest.status.in_((STATUS_REQUESTED, STATUS_CONFIRMED)),
    ).first():
        raise DeletionConflict("A deletion request is already pending.")

    if enforce_rate_limit:
        allowed, retry_after = ratelimit.consume(
            f"data-deletion:request:user:{user.id}",
            max_hits=REQUEST_LIMIT,
            window=REQUEST_WINDOW_SECONDS,
        )
        if not allowed:
            raise RateLimited(retry_after)

    row = UserDeletionRequest(
        user_id=user.id, status=STATUS_REQUESTED, mode=normalized, requested_at=moment
    )
    db.session.add(row)
    db.session.flush()
    audit.record(
        audit.USER_DELETION_REQUESTED,
        user=user,
        target_type="user_deletion_request",
        target_id=row.id,
        metadata={"status": STATUS_REQUESTED, "mode": normalized},
    )
    if commit:
        db.session.commit()
    return row


def confirm_request(
    request_id: int,
    user,
    confirmation: str | None,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> tuple[UserDeletionRequest, dict]:
    """Confirm a request with the exact phrase, then apply it.

    Returns the completed request and a summary of what was removed. The
    confirmation is checked before the state transition, so a missing phrase
    never changes anything.
    """
    if not isinstance(confirmation, str) or confirmation.strip().lower() != CONFIRMATION_PHRASE:
        raise ConfirmationMismatch(
            f'Confirmation required. Submit "{CONFIRMATION_PHRASE}" to continue.'
        )
    row = _get_owned(request_id, user)
    moment = _now(now)
    if row.status == STATUS_COMPLETED:
        raise AlreadyCompleted("This deletion request is already complete.")
    if row.status == STATUS_CANCELLED:
        raise DeletionConflict("This deletion request was cancelled.")
    if row.status != STATUS_REQUESTED:
        raise DeletionConflict(f"Cannot confirm a {row.status} deletion request.")

    row.status = STATUS_CONFIRMED
    row.confirmed_at = moment
    audit.record(
        audit.USER_DELETION_CONFIRMED,
        user=user,
        target_type="user_deletion_request",
        target_id=row.id,
        metadata={"status": STATUS_CONFIRMED, "mode": row.mode},
    )
    summary = complete_request(row.id, user, now=moment, commit=False)
    if commit:
        db.session.commit()
    return row, summary


def cancel_request(
    request_id: int,
    user,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> UserDeletionRequest:
    """Cancel a pending request without changing any account data."""
    row = _get_owned(request_id, user)
    if row.status not in (STATUS_REQUESTED, STATUS_CONFIRMED):
        raise DeletionConflict(f"Cannot cancel a {row.status} deletion request.")
    row.status = STATUS_CANCELLED
    row.cancelled_at = _now(now)
    audit.record(
        audit.USER_DELETION_CANCELLED,
        user=user,
        target_type="user_deletion_request",
        target_id=row.id,
        metadata={"status": STATUS_CANCELLED, "mode": row.mode},
    )
    if commit:
        db.session.commit()
    return row


def complete_request(
    request_id: int,
    user,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Apply a confirmed request and return a summary of removed rows."""
    row = _get_owned(request_id, user)
    if row.status == STATUS_COMPLETED:
        raise AlreadyCompleted("This deletion request is already complete.")
    if row.status != STATUS_CONFIRMED:
        raise DeletionConflict(f"Cannot complete a {row.status} deletion request.")
    moment = _now(now)

    if legal_hold.user_has_active_hold(user, legal_hold.SCOPE_WORKSPACE):
        raise LegalHoldActive(
            "A legal hold covers data in this account. Release the hold before deleting."
        )

    summary = {"mode": row.mode, "deleted": {}, "anonymized": False}
    if row.mode == MODE_DELETION:
        summary["deleted"] = _delete_personal_content(user)

    # Record the decision before mutating the profile so the audit row keeps
    # the account reference it was written with.
    row.status = STATUS_COMPLETED
    row.completed_at = moment
    audit.record(
        audit.USER_DELETION_COMPLETED,
        user=user,
        target_type="user_deletion_request",
        target_id=row.id,
        metadata={
            "status": STATUS_COMPLETED,
            "mode": row.mode,
            "deleted": summary["deleted"],
        },
    )
    _anonymize_profile(user)
    _remove_memberships(user)
    summary["anonymized"] = True
    if commit:
        db.session.commit()
    return summary


def _delete_personal_content(user) -> dict:
    """Delete content rows owned by ``user`` and return per-table counts."""
    counts = {
        "conversations": 0,
        "prompts": 0,
        "prompt_versions": 0,
        "api_keys": 0,
        "github_accounts": 0,
    }
    for conversation in Conversation.query.filter_by(user_id=user.id).all():
        db.session.delete(conversation)
        counts["conversations"] += 1
    prompt_ids = [
        row.id for row in Prompt.query.with_entities(Prompt.id).filter_by(user_id=user.id).all()
    ]
    if prompt_ids:
        counts["prompt_versions"] += PromptVersion.query.filter(
            PromptVersion.prompt_id.in_(prompt_ids)
        ).delete(synchronize_session=False)
    counts["prompt_versions"] += PromptVersion.query.filter_by(changed_by=user.id).delete(
        synchronize_session=False
    )
    counts["prompts"] = Prompt.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    counts["api_keys"] = ApiKey.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    counts["github_accounts"] = GithubAccount.query.filter_by(user_id=user.id).delete(
        synchronize_session=False
    )
    # Export rows contain request metadata and one-time links; drop them so a
    # stale link can never be used after deletion.
    UserDataExport.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    return counts


def _anonymize_profile(user) -> None:
    """Clear personal profile fields and disable the account (idempotent)."""
    user.username = f"deleted-user-{user.id}"
    user.email = f"deleted-{user.id}@deleted.invalid"
    user.password_hash = generate_password_hash(secrets.token_urlsafe(32))
    user.is_active = False
    user.mfa_secret = None
    user.mfa_enabled = False
    user.stellar_network = None
    user.last_login_at = None


def _remove_memberships(user) -> None:
    """Soft-remove every active membership so access stops immediately."""
    memberships = WorkspaceMember.query.filter_by(user_id=user.id).all()
    for membership in memberships:
        if membership.is_active_member:
            previous_role = membership.role
            membership.mark_removed()
            record_role_change(
                workspace_id=membership.workspace_id,
                actor=user,
                target_user=user,
                previous_role=previous_role,
                new_role=None,
                membership_id=membership.id,
            )


__all__ = [
    "CONFIRMATION_PHRASE",
    "MODE_ANONYMIZATION",
    "MODE_DELETION",
    "REQUEST_LIMIT",
    "REQUEST_WINDOW_SECONDS",
    "AlreadyCompleted",
    "ConfirmationMismatch",
    "DeletionConflict",
    "DeletionError",
    "LegalHoldActive",
    "RateLimited",
    "cancel_request",
    "complete_request",
    "confirm_request",
    "list_requests",
    "request_deletion",
]
