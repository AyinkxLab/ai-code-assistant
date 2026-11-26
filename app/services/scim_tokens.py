"""SCIM bearer token lifecycle and authentication (#594).

Tokens belong to an organization owner (``users.id``) and carry a default
provisioning workspace. The secret is generated once, shown only in the
issue/rotate response, and stored solely as a SHA-256 hash. Missing, revoked,
and expired tokens all fail closed in :func:`authenticate`.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from app.api.error_catalog import CONFLICT, INVALID_INPUT, NOT_FOUND
from app.extensions import db
from app.models.scim_token import ScimToken

SECRET_PREFIX = "sct_"


class ScimTokenError(ValueError):
    """An invalid SCIM token operation with a public catalog code."""

    def __init__(self, message: str, code: str = INVALID_INPUT) -> None:
        super().__init__(message)
        self.code = code


def hash_secret(secret: str) -> str:
    """Return the SHA-256 hex digest stored for ``secret``."""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _new_secret() -> str:
    return f"{SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def _normalize_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        return expires_at.replace(tzinfo=UTC)
    return expires_at.astimezone(UTC)


def _normalize_label(label) -> str | None:
    if label is None:
        return None
    cleaned = str(label).strip()
    if not cleaned:
        return None
    if len(cleaned) > 100:
        raise ScimTokenError("A token label must be at most 100 characters.")
    return cleaned


def list_tokens(owner_id: int) -> list[ScimToken]:
    """Return the owner's tokens, newest first."""
    return (
        ScimToken.query.filter_by(owner_id=owner_id)
        .order_by(ScimToken.created_at.desc(), ScimToken.id.desc())
        .all()
    )


def get_owned(owner_id: int, token_id: int) -> ScimToken | None:
    """Return the owner's token or ``None`` (no cross-organization access)."""
    return ScimToken.query.filter_by(id=token_id, owner_id=owner_id).first()


def create_token(
    owner_id: int,
    workspace_id: int,
    *,
    created_by_id: int | None = None,
    label=None,
    expires_at: datetime | None = None,
) -> tuple[ScimToken, str]:
    """Issue a token and return ``(row, secret)``; the secret is shown once."""
    if not workspace_id:
        raise ScimTokenError("A default provisioning workspace is required.")
    secret = _new_secret()
    row = ScimToken(
        owner_id=owner_id,
        workspace_id=workspace_id,
        label=_normalize_label(label),
        prefix=secret[:12],
        token_hash=hash_secret(secret),
        created_by_id=created_by_id,
        expires_at=_normalize_expiry(expires_at),
    )
    db.session.add(row)
    db.session.commit()
    return row, secret


def rotate_token(
    owner_id: int, token_id: int, *, rotated_by_id: int | None = None
) -> tuple[ScimToken, str] | None:
    """Replace a token's secret; return ``None`` when the token is not owned."""
    row = get_owned(owner_id, token_id)
    if row is None:
        return None
    if row.revoked_at is not None:
        raise ScimTokenError("A revoked token cannot be rotated.", code=CONFLICT)
    secret = _new_secret()
    row.prefix = secret[:12]
    row.token_hash = hash_secret(secret)
    # The actor is intentionally not persisted on rotation; the audit entry in
    # the route carries the bounded fact without widening the token table.
    db.session.commit()
    return row, secret


def revoke_token(
    owner_id: int, token_id: int, *, revoked_by_id: int | None = None
) -> ScimToken | None:
    """Revoke a token while keeping its row for audit history."""
    row = get_owned(owner_id, token_id)
    if row is None or row.revoked_at is not None:
        return None
    row.revoked_at = datetime.now(UTC)
    row.revoked_by_id = revoked_by_id
    db.session.commit()
    return row


def authenticate(secret: str | None) -> ScimToken | None:
    """Return the active token matching ``secret``, recording its last use.

    Empty, unknown, revoked, and expired tokens all return ``None``; no
    distinction is exposed to the caller.
    """
    secret = (secret or "").strip()
    if not secret:
        return None
    row = ScimToken.query.filter_by(token_hash=hash_secret(secret)).first()
    if row is None or not row.is_active:
        return None
    row.last_used_at = datetime.now(UTC)
    db.session.commit()
    return row


def get_owned_or_error(owner_id: int, token_id: int) -> ScimToken:
    """Return the owner's token or raise a ``not_found`` catalog error."""
    row = get_owned(owner_id, token_id)
    if row is None:
        raise ScimTokenError("SCIM token not found.", code=NOT_FOUND)
    return row


__all__ = [
    "SECRET_PREFIX",
    "ScimTokenError",
    "authenticate",
    "create_token",
    "get_owned",
    "get_owned_or_error",
    "hash_secret",
    "list_tokens",
    "revoke_token",
    "rotate_token",
]
