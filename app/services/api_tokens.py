"""Scoped API token lifecycle and request authentication (#616).

Tokens are personal access tokens for the public JSON API:

* the secret is generated once and only its SHA-256 hash is stored;
* scopes are ranked ``read`` < ``write`` < ``admin`` and enforced on every
  request that authenticates with a token;
* ``read`` allows safe methods, ``write`` adds POST/PUT/PATCH, and ``admin``
  adds DELETE (destructive operations).

Login sessions keep working unchanged. A bearer token only authenticates
requests under a JSON API path (``/api/...`` or ``.../api/...``); using a token
against an HTML route is rejected with the structured ``unauthorized`` error.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from flask import g, request, session

from app.api.error_catalog import CONFLICT, FORBIDDEN, INVALID_INPUT, UNAUTHORIZED, error_response
from app.extensions import db
from app.models.api_token import ADMIN, READ, SCOPE_RANK, SCOPES, WRITE, ApiToken

SECRET_PREFIX = "aat_"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiTokenError(ValueError):
    """Raised for invalid token input or a disallowed lifecycle transition."""

    def __init__(self, message: str, code: str = INVALID_INPUT) -> None:
        super().__init__(message)
        self.code = code


def hash_secret(secret: str) -> str:
    """Return the SHA-256 hex digest stored for ``secret``."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_scopes(scopes) -> str:
    """Validate and canonicalize a scope iterable (or comma-separated string)."""
    if isinstance(scopes, str):
        requested = {part.strip() for part in scopes.split(",") if part.strip()}
    else:
        requested = {str(part).strip() for part in (scopes or []) if str(part).strip()}
    unknown = requested - set(SCOPES)
    if unknown:
        raise ApiTokenError(f"Unknown scope(s): {', '.join(sorted(unknown))}.")
    if not requested:
        raise ApiTokenError("At least one scope is required.")
    return ",".join(scope for scope in SCOPES if scope in requested)


def _new_secret() -> str:
    return f"{SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def _normalize_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        return expires_at.replace(tzinfo=UTC)
    return expires_at.astimezone(UTC)


def list_tokens(user) -> list[ApiToken]:
    """Return ``user``'s tokens, newest first."""
    return ApiToken.query.filter_by(user_id=user.id).order_by(ApiToken.created_at.desc()).all()


def get_owned(user, token_id: int) -> ApiToken | None:
    """Return ``user``'s token or ``None`` (owner scoping)."""
    return ApiToken.query.filter_by(id=token_id, user_id=user.id).first()


def create_token(user, *, label=None, scopes, expires_at: datetime | None = None):
    """Create a token and return ``(row, secret)``; the secret is shown once."""
    secret = _new_secret()
    row = ApiToken(
        user_id=user.id,
        label=(str(label).strip()[:100] or None) if label else None,
        prefix=secret[:12],
        token_hash=hash_secret(secret),
        scopes=normalize_scopes(scopes),
        expires_at=_normalize_expiry(expires_at),
    )
    db.session.add(row)
    db.session.commit()
    return row, secret


def rotate_token(user, token_id: int):
    """Replace a token's secret and return ``(row, new_secret)``.

    The row (and therefore its audit history and metadata) is kept; the old
    secret stops working immediately. A revoked token cannot be rotated.
    """
    row = get_owned(user, token_id)
    if row is None:
        return None
    if row.revoked_at is not None:
        raise ApiTokenError("A revoked token cannot be rotated.", code=CONFLICT)
    secret = _new_secret()
    row.prefix = secret[:12]
    row.token_hash = hash_secret(secret)
    db.session.commit()
    return row, secret


def revoke_token(user, token_id: int) -> ApiToken | None:
    """Revoke a token, keeping its row for audit history."""
    row = get_owned(user, token_id)
    if row is None or row.revoked_at is not None:
        return None
    row.revoked_at = datetime.now(UTC)
    db.session.commit()
    return row


def authenticate(secret: str) -> ApiToken | None:
    """Return the active token matching ``secret``, recording its last use."""
    secret = (secret or "").strip()
    if not secret:
        return None
    row = ApiToken.query.filter_by(token_hash=hash_secret(secret)).first()
    if row is None or not row.is_active:
        return None
    owner = row_user(row)
    if owner is None or not owner.is_active:
        return None
    row.last_used_at = datetime.now(UTC)
    db.session.commit()
    return row


def row_user(row: ApiToken):
    """Return the owning user (loaded once per request)."""
    from app.models import User

    return db.session.get(User, row.user_id)


def required_scope(method: str) -> str:
    """Return the minimum scope needed for an HTTP ``method``."""
    if method in SAFE_METHODS:
        return READ
    if method == "DELETE":
        return ADMIN
    return WRITE


def token_allows(token: ApiToken, scope: str) -> bool:
    """Return ``True`` when ``token`` grants ``scope`` (ranked hierarchy)."""
    return any(SCOPE_RANK[granted] >= SCOPE_RANK[scope] for granted in token.scope_list)


def _is_api_path(path: str) -> bool:
    """Return whether a bearer token is accepted on ``path``.

    The minimal SCIM Groups surface (#593) reuses the same hash-only personal
    access tokens as the JSON API; no separate SCIM secret is introduced.
    """
    return (
        path == "/api"
        or path.startswith("/api/")
        or "/api/" in path
        or path == "/scim/v2"
        or path.startswith("/scim/v2/")
    )


def _header_secret() -> str | None:
    """Read a token from ``Authorization: Bearer`` or ``X-API-Token``."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("X-API-Token", "").strip() or None


def init_api_tokens(app) -> None:
    """Install bearer authentication and the per-request scope guard.

    Registered early in the factory (before the other ``before_request`` hooks)
    so the token identity is available to every later guard. Flask-Login
    resolves ``current_user`` lazily from ``g._login_user``; setting it here
    authenticates the request without touching the session, so no cookie is
    issued and no session CSRF token is needed.
    """

    @app.before_request
    def _authenticate_and_enforce_scope():
        g.api_token = None
        secret = _header_secret()
        if secret is None:
            return None
        if not _is_api_path(request.path):
            return error_response(UNAUTHORIZED, "API tokens are accepted on JSON API routes only.")
        token = authenticate(secret)
        if token is None:
            # A session-authenticated caller may still send an unrelated
            # Authorization header (the quota limiter uses it as a subject key);
            # without a session an unmatched token is an authentication failure.
            if session.get("_user_id"):
                return None
            return error_response(UNAUTHORIZED)
        g.api_token = token
        # Flask-Login's ``current_user`` reads this request-scoped value.
        g._login_user = row_user(token)
        if not token_allows(token, required_scope(request.method)):
            return error_response(FORBIDDEN, "This token does not grant the required scope.")
        return None


def current_token() -> ApiToken | None:
    """Return the token authenticating the current request, if any."""
    return getattr(g, "api_token", None)


__all__ = [
    "ApiTokenError",
    "authenticate",
    "create_token",
    "current_token",
    "get_owned",
    "hash_secret",
    "init_api_tokens",
    "list_tokens",
    "normalize_scopes",
    "required_scope",
    "revoke_token",
    "rotate_token",
    "token_allows",
]
