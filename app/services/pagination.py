"""Bounded page and stable-cursor helpers for list endpoints (issue #700).

The contract is deliberately small and shared by every endpoint that exposes
large workspace collections:

* ``page`` is 1-based; values below ``1`` are treated as ``1``.
* ``limit`` defaults to :data:`DEFAULT_PAGE_SIZE` and is clamped to
  :data:`MAX_PAGE_SIZE`, so a response can never be unbounded.
* ``cursor`` is an opaque, URL-safe token returned as ``next_cursor``. It
  continues a query *after* the last row of the previous page, so rows inserted
  between requests do not shift later pages (unlike a bare offset).
* A non-integer ``page``/``limit`` or a malformed cursor raises
  :class:`PaginationError`; routes translate that into a structured ``400``
  instead of letting a stack trace escape.

Cursors are meant to be opaque to clients. Callers decide which stable key they
encode (for example ``{"id": 42}``) and validate its shape before using it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

#: Maximum accepted cursor length; anything larger is treated as malformed.
MAX_CURSOR_CHARS = 512


class PaginationError(ValueError):
    """Raised for an invalid page, page size, or cursor."""

    code = "invalid_pagination"


@dataclass(frozen=True)
class PageRequest:
    """A validated page request."""

    page: int
    limit: int
    cursor: dict[str, Any] | None = None


def _parse_int(args: Mapping[str, Any], name: str, default: int) -> int:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise PaginationError(f"{name} must be an integer.") from None


def encode_cursor(values: Mapping[str, Any]) -> str:
    """Encode a stable cursor dictionary as an opaque URL-safe token."""
    raw = json.dumps(dict(values), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode a cursor produced by :func:`encode_cursor`."""
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise PaginationError("Invalid cursor.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, TypeError):
        raise PaginationError("Invalid cursor.") from None
    if not isinstance(data, dict):
        raise PaginationError("Invalid cursor.")
    return data


def parse_page_request(
    args: Mapping[str, Any],
    *,
    default_limit: int = DEFAULT_PAGE_SIZE,
    max_limit: int = MAX_PAGE_SIZE,
) -> PageRequest:
    """Validate ``page``, ``limit``, and ``cursor`` query arguments.

    Oversized limits are clamped (the documented maximum is enforced without
    rejecting a client that asked for too much); malformed values raise
    :class:`PaginationError`.
    """
    page = max(1, _parse_int(args, "page", 1))
    limit = max(1, min(_parse_int(args, "limit", default_limit), max_limit))
    raw_cursor = args.get("cursor")
    cursor = decode_cursor(raw_cursor) if raw_cursor else None
    return PageRequest(page=page, limit=limit, cursor=cursor)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_CURSOR_CHARS",
    "MAX_PAGE_SIZE",
    "PageRequest",
    "PaginationError",
    "decode_cursor",
    "encode_cursor",
    "parse_page_request",
]
