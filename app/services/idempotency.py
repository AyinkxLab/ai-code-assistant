"""Retry-safe POST handling with persisted idempotency keys (#649).

Clients send an ``Idempotency-Key`` header. The first request reserves the key
and stores a hash of the canonical request body before the view runs. A retry
with the same key and body replays the stored response; reusing the key with a
different body is rejected with a structured ``409`` conflict. A retry that
arrives while the first request is still running also receives ``409`` rather
than executing the side effect twice.

Only a SHA-256 digest of the request body is stored. Response bodies are kept
for :data:`DEFAULT_TTL_SECONDS` (configurable with
``IDEMPOTENCY_TTL_SECONDS``) and expired rows are removed on every access.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from flask import Response, current_app, g, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.api.error_catalog import CONFLICT, INVALID_INPUT, error_response
from app.extensions import db
from app.models import IdempotencyKey

HEADER = "Idempotency-Key"
ALT_HEADER = "X-Idempotency-Key"
REPLAY_HEADER = "Idempotency-Replayed"
ALT_REPLAY_HEADER = "X-Idempotency-Replayed"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_KEY_LENGTH = 255

_KEY_RE = re.compile(r"^[A-Za-z0-9._~:/+=-]+$")
_REPLAY_HEADERS = frozenset(
    {
        "Content-Type",
        "Content-Language",
        "Location",
        "X-Export-Limit",
        "X-Request-ID",
        "X-Trace-ID",
    }
)


class IdempotencyError(Exception):
    """Base class for a rejected idempotency request."""


class InvalidKey(IdempotencyError):
    """The supplied key is missing or malformed."""


class KeyReuse(IdempotencyError):
    """The key was already used with a different request body."""


class RequestInProgress(IdempotencyError):
    """Another request currently holds the same key."""


def _presented_key() -> str | None:
    """Return the key header, accepting the X- form as a compatibility alias."""
    return request.headers.get(HEADER) or request.headers.get(ALT_HEADER)


def validate_key(raw: str | None) -> str:
    """Return a valid key, or raise :class:`InvalidKey`."""
    key = (raw or "").strip()
    if not key:
        raise InvalidKey(f"{HEADER} is required.")
    if len(key) > MAX_KEY_LENGTH or not _KEY_RE.match(key):
        raise InvalidKey(
            f"{HEADER} must be 1-{MAX_KEY_LENGTH} characters from " "A-Z, a-z, 0-9, and ._~:/+= -."
        )
    return key


def canonical_request_hash(body: bytes, mimetype: str | None = None) -> str:
    """Hash the request body deterministically.

    JSON bodies are canonicalized (sorted keys, compact separators) so a client
    that serializes the same object differently is still treated as a retry.
    Malformed JSON falls back to the raw bytes.
    """
    payload = body or b""
    if mimetype and "json" in mimetype.lower():
        try:
            parsed = json.loads(payload.decode("utf-8"))
            payload = json.dumps(
                parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        except (UnicodeDecodeError, ValueError):
            pass
    return hashlib.sha256(payload).hexdigest()


def _ttl_seconds() -> int:
    try:
        value = int(current_app.config.get("IDEMPOTENCY_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


def _owner() -> tuple[str, int | None]:
    try:
        if getattr(current_user, "is_authenticated", False):
            return f"user:{current_user.id}", current_user.id
    except Exception:  # bare Flask apps used by unit tests have no login manager
        pass
    return "anonymous", None


def _endpoint() -> str:
    rule = request.url_rule.rule if request.url_rule else request.path
    return rule[:255]


def _now() -> datetime:
    return datetime.now(UTC)


def purge_expired() -> int:
    """Delete expired rows and return how many were removed."""
    now = _now()
    expired = IdempotencyKey.query.filter(IdempotencyKey.expires_at <= now).all()
    if not expired:
        return 0
    for row in expired:
        db.session.delete(row)
    db.session.commit()
    return len(expired)


def _lookup(owner: str, endpoint: str, key: str) -> IdempotencyKey | None:
    return IdempotencyKey.query.filter_by(owner=owner, endpoint=endpoint, key=key).first()


def _conflict(message: str, *, details=None):
    return error_response(CONFLICT, message, details=details)


def _existing_response(row: IdempotencyKey) -> Response:
    headers = {}
    if row.response_headers:
        try:
            parsed = json.loads(row.response_headers)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            headers = {str(name): str(value) for name, value in parsed.items()}
    response = Response(
        row.response_body or "",
        status=row.status_code or 200,
        headers=headers,
    )
    response.headers[REPLAY_HEADER] = "true"
    response.headers[ALT_REPLAY_HEADER] = "true"
    return response


def begin() -> Response | None:
    """Reserve the current request's key.

    Returns a response when the request must be answered immediately (a
    replay or an error), or ``None`` when the caller should run the view.
    """
    raw = _presented_key()
    if raw is None:
        return None
    try:
        key = validate_key(raw)
    except InvalidKey as exc:
        return error_response(INVALID_INPUT, str(exc))

    # Expired keys must never block a fresh request with the same value.
    purge_expired()

    body = request.get_data(cache=True)
    request_hash = canonical_request_hash(body, request.content_type)
    owner, user_id = _owner()
    endpoint = _endpoint()

    existing = _lookup(owner, endpoint, key)
    if existing is not None:
        return _resolve_existing(existing, request_hash)

    row = IdempotencyKey(
        owner=owner,
        user_id=user_id,
        endpoint=endpoint,
        key=key,
        request_hash=request_hash,
        expires_at=_now() + timedelta(seconds=_ttl_seconds()),
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = _lookup(owner, endpoint, key)
        if existing is None:  # pragma: no cover - concurrent delete
            return _conflict("The request is already being processed.")
        return _resolve_existing(existing, request_hash)

    g.idempotency_row_id = row.id
    return None


def _resolve_existing(row: IdempotencyKey, request_hash: str) -> Response:
    if row.request_hash != request_hash:
        return _conflict("This idempotency key was already used with a different request body.")
    if not row.is_complete:
        return _conflict("A request with this idempotency key is still in progress.")
    return _existing_response(row)


def finish(response: Response) -> Response:
    """Store the response for the reserved key and return it unchanged."""
    row_id = g.pop("idempotency_row_id", None)
    if row_id is None:
        return response

    row = db.session.get(IdempotencyKey, row_id)
    if row is None:
        # A route rollback may have removed the reservation. Recreate it so a
        # later retry still replays this response rather than repeating work.
        owner, user_id = _owner()
        raw_key = _presented_key()
        try:
            key = validate_key(raw_key)
        except InvalidKey:  # pragma: no cover - begin() already validated
            return response
        row = IdempotencyKey(
            owner=owner,
            user_id=user_id,
            endpoint=_endpoint(),
            key=key,
            request_hash=canonical_request_hash(request.get_data(cache=True), request.content_type),
            expires_at=_now() + timedelta(seconds=_ttl_seconds()),
        )
        db.session.add(row)

    row.status_code = response.status_code
    if not response.is_streamed and not response.direct_passthrough:
        try:
            row.response_body = response.get_data(as_text=True)
        except (UnicodeDecodeError, RuntimeError):  # pragma: no cover - binary body
            row.response_body = None
    row.response_headers = json.dumps(
        {name: value for name, value in response.headers.items() if name in _REPLAY_HEADERS},
        sort_keys=True,
    )
    try:
        db.session.commit()
    except Exception:  # pragma: no cover - storage must not break the response
        db.session.rollback()
    return response


def is_managed_endpoint() -> bool:
    """Return True when the target view handles idempotency itself."""
    endpoint = request.endpoint
    if endpoint is None:
        return False
    view = current_app.view_functions.get(endpoint)
    return bool(getattr(view, "_idempotent_managed", False))


def required() -> bool:
    endpoint = request.endpoint
    if endpoint is None:
        return False
    view = current_app.view_functions.get(endpoint)
    return bool(getattr(view, "_idempotency_required", False))


def idempotent(view):
    """Handle an ``Idempotency-Key`` even without the application hooks."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        early = begin()
        if early is not None:
            return early
        response = view(*args, **kwargs)
        if not isinstance(response, Response):
            response = current_app.make_response(response)
        return finish(response)

    wrapped._idempotent_managed = True
    return wrapped


def idempotent_required(view):
    """Require a valid key and make the view retry safe."""
    retry_safe = idempotent(view)

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        try:
            validate_key(_presented_key())
        except InvalidKey as exc:
            return error_response(INVALID_INPUT, str(exc))
        return retry_safe(*args, **kwargs)

    wrapped._idempotent_managed = True
    wrapped._idempotency_required = True
    return wrapped


require_idempotency_key = idempotent_required
idempotency_required = idempotent_required


__all__ = [
    "ALT_HEADER",
    "ALT_REPLAY_HEADER",
    "DEFAULT_TTL_SECONDS",
    "HEADER",
    "REPLAY_HEADER",
    "IdempotencyError",
    "InvalidKey",
    "KeyReuse",
    "RequestInProgress",
    "begin",
    "canonical_request_hash",
    "finish",
    "idempotency_required",
    "idempotent",
    "idempotent_required",
    "purge_expired",
    "require_idempotency_key",
    "validate_key",
]


def register_idempotency(app) -> None:
    """Install the POST hooks and the maintenance CLI command."""

    @app.before_request
    def _begin_idempotency():
        if request.method != "POST" or is_managed_endpoint():
            return None
        if required() and _presented_key() is None:
            return error_response(INVALID_INPUT, f"{HEADER} is required.")
        return begin()

    @app.after_request
    def _finish_idempotency(response):
        if request.method == "POST" and not is_managed_endpoint():
            return finish(response)
        return response

    @app.cli.command("purge-idempotency")
    def purge_idempotency_command():
        """Delete expired idempotency keys."""
        _print_purge_result()

    @app.cli.group("idempotency")
    def idempotency_group():
        """Manage persisted POST idempotency keys."""

    @idempotency_group.command("purge")
    def purge_idempotency_group_command():
        """Delete expired idempotency keys."""
        _print_purge_result()


def _print_purge_result() -> None:
    removed = purge_expired()
    print(f"Removed {removed} expired idempotency key(s).")
