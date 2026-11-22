"""User data export request workflow (#614).

Requesting an export is rate limited, every state change is audited, and the
download link is a one-time token whose SHA-256 hash is all that is stored. A
link expires after :data:`DOWNLOAD_TTL` (24 hours by default) and an expired or
already-downloaded link cannot be reused.

The exported document contains the requesting account's profile, conversations,
messages, and saved prompts. It deliberately never includes the password hash,
API keys, session data, or any other user's records.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import Conversation, Prompt, UserDataExport
from app.models.user_data_export import (
    STATUS_DOWNLOADED,
    STATUS_EXPIRED,
    STATUS_PROCESSING,
    STATUS_READY,
    STATUS_REQUESTED,
    hash_download_token,
)
from app.services import audit, ratelimit

#: Requests allowed per user per window. Kept as module constants so tests can
#: tighten them without changing the environment.
REQUEST_LIMIT = 3
REQUEST_WINDOW_SECONDS = 24 * 60 * 60
DOWNLOAD_TTL = timedelta(hours=24)

_TRANSITIONS = {
    STATUS_REQUESTED: {STATUS_PROCESSING},
    STATUS_PROCESSING: {STATUS_READY},
    STATUS_READY: {STATUS_DOWNLOADED, STATUS_EXPIRED},
    STATUS_DOWNLOADED: set(),
    STATUS_EXPIRED: set(),
}


class DataExportError(Exception):
    """Base class for a failed export workflow operation."""


class RateLimited(DataExportError):
    """The caller has requested too many exports in the current window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many data export requests. Please retry later.")
        self.retry_after = retry_after


class ExportExpired(DataExportError):
    """The download link is past its expiry."""


class ExportAlreadyDownloaded(DataExportError):
    """The one-time download link has already been used."""


def _now(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _transition(row: UserDataExport, status: str, moment: datetime) -> None:
    if status not in _TRANSITIONS.get(row.status, set()):
        raise DataExportError(f"Cannot change a {row.status} export to {status}.")
    row.status = status
    if status == STATUS_PROCESSING:
        row.processing_started_at = moment
    elif status == STATUS_READY:
        row.completed_at = moment
    elif status == STATUS_DOWNLOADED:
        row.downloaded_at = moment


def list_requests(user, *, limit: int = 20) -> list[UserDataExport]:
    """Return the user's most recent export requests."""
    return (
        UserDataExport.query.filter_by(user_id=user.id)
        .order_by(UserDataExport.requested_at.desc(), UserDataExport.id.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


def request_export(
    user,
    *,
    now: datetime | None = None,
    enforce_rate_limit: bool = True,
    commit: bool = True,
) -> UserDataExport:
    """Create a ``requested`` export row for ``user``.

    Raises :class:`RateLimited` when the per-user window is exhausted. The audit
    entry is appended in the same transaction as the row.
    """
    if user is None or getattr(user, "id", None) is None:
        raise ValueError("A user is required to request an export.")
    moment = _now(now)

    if enforce_rate_limit:
        allowed, retry_after = ratelimit.consume(
            f"data-export:request:user:{user.id}",
            max_hits=REQUEST_LIMIT,
            window=REQUEST_WINDOW_SECONDS,
        )
        if not allowed:
            raise RateLimited(retry_after)

    row = UserDataExport(user_id=user.id, status=STATUS_REQUESTED, requested_at=moment)
    db.session.add(row)
    db.session.flush()
    audit.record(
        audit.DATA_EXPORT_REQUESTED,
        user=user,
        target_type="user_data_export",
        target_id=row.id,
        metadata={"status": STATUS_REQUESTED},
    )
    if commit:
        db.session.commit()
    return row


def process_request(
    request_id: int,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> tuple[UserDataExport, str]:
    """Move a pending request to ``ready`` and return its one-time token.

    The plaintext token is returned to the caller and never persisted.
    """
    row = db.session.get(UserDataExport, request_id)
    if row is None:
        raise LookupError("Data export request not found.")
    moment = _now(now)
    _transition(row, STATUS_PROCESSING, moment)

    token = secrets.token_urlsafe(32)
    _transition(row, STATUS_READY, moment)
    row.token_hash = hash_download_token(token)
    row.expires_at = moment + DOWNLOAD_TTL
    audit.record(
        audit.DATA_EXPORT_READY,
        user_id=row.user_id,
        target_type="user_data_export",
        target_id=row.id,
        metadata={"status": STATUS_READY},
    )
    if commit:
        db.session.commit()
    return row, token


def build_export_payload(user, *, now: datetime | None = None) -> dict:
    """Return the account-data document for ``user``.

    Conversations and prompts are the user's own records; no secret material
    (password hash, tokens, API keys) is included.
    """
    moment = _now(now)
    conversations = (
        Conversation.query.filter_by(user_id=user.id)
        .order_by(Conversation.created_at.asc(), Conversation.id.asc())
        .all()
    )
    prompts = (
        Prompt.query.filter_by(user_id=user.id)
        .order_by(Prompt.created_at.asc(), Prompt.id.asc())
        .all()
    )
    return {
        "schema_version": 1,
        "kind": "user-data-export",
        "generated_at": moment.isoformat(),
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_active": bool(user.is_active),
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        },
        "conversations": [
            {
                "id": conversation.id,
                "title": conversation.title,
                "created_at": (
                    conversation.created_at.isoformat() if conversation.created_at else None
                ),
                "messages": [
                    {
                        "id": message.id,
                        "role": message.role,
                        "content": message.content,
                        "created_at": (
                            message.created_at.isoformat() if message.created_at else None
                        ),
                    }
                    for message in conversation.messages
                ],
            }
            for conversation in conversations
        ],
        "prompts": [prompt.to_dict() for prompt in prompts],
    }


def expire_if_needed(row: UserDataExport, *, now: datetime | None = None) -> bool:
    """Mark a ready request expired when its window has passed; return whether it did."""
    if row.status != STATUS_READY or row.expires_at is None:
        return False
    if _now(now) < _now(row.expires_at):
        return False
    _transition(row, STATUS_EXPIRED, _now(now))
    audit.record(
        audit.DATA_EXPORT_EXPIRED,
        user_id=row.user_id,
        target_type="user_data_export",
        target_id=row.id,
        metadata={"status": STATUS_EXPIRED},
    )
    return True


def download_export(
    request_id: int,
    token: str,
    user,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Validate a one-time link and return the export document.

    Unknown requests and requests owned by another user raise ``LookupError`` so
    callers cannot probe for other users' request ids. An expired link raises
    :class:`ExportExpired`; an already-used link raises
    :class:`ExportAlreadyDownloaded`.
    """
    if not isinstance(token, str) or not token:
        raise ValueError("A download token is required.")
    row = db.session.get(UserDataExport, request_id)
    if row is None or row.user_id != user.id:
        raise LookupError("Data export request not found.")
    if not row.token_hash or not hmac.compare_digest(row.token_hash, hash_download_token(token)):
        raise LookupError("Data export request not found.")

    moment = _now(now)
    if row.status == STATUS_READY and expire_if_needed(row, now=moment):
        if commit:
            db.session.commit()
        raise ExportExpired("This export download link has expired.")
    if row.status == STATUS_EXPIRED:
        raise ExportExpired("This export download link has expired.")
    if row.status == STATUS_DOWNLOADED:
        raise ExportAlreadyDownloaded("This export download link has already been used.")
    if row.status != STATUS_READY:
        raise DataExportError("The export is not ready for download.")

    payload = build_export_payload(row.user, now=moment)
    _transition(row, STATUS_DOWNLOADED, moment)
    audit.record(
        audit.DATA_EXPORT_DOWNLOADED,
        user_id=row.user_id,
        target_type="user_data_export",
        target_id=row.id,
        metadata={"status": STATUS_DOWNLOADED},
    )
    if commit:
        db.session.commit()
    return payload


__all__ = [
    "DOWNLOAD_TTL",
    "REQUEST_LIMIT",
    "REQUEST_WINDOW_SECONDS",
    "DataExportError",
    "ExportAlreadyDownloaded",
    "ExportExpired",
    "RateLimited",
    "build_export_payload",
    "download_export",
    "expire_if_needed",
    "list_requests",
    "process_request",
    "request_export",
]
