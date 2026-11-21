"""Webhook signature helpers for the AI Code Assistant Python SDK (#663).

The server signs each delivery over::

    <timestamp>.<raw body bytes>

with HMAC-SHA256 and sends::

    X-AICA-Signature: v1=<lowercase hex>
    X-AICA-Timestamp: <unix seconds>

Call :func:`verify_signature` from a webhook endpoint before trusting the body.
The comparison is constant time and the timestamp is checked against a replay
tolerance window.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

from .errors import ValidationError

SIGNATURE_HEADER = "X-AICA-Signature"
TIMESTAMP_HEADER = "X-AICA-Timestamp"
SIGNATURE_VERSION = "v1"
DEFAULT_TOLERANCE_SECONDS = 300

_BODY_TYPES = (bytes, bytearray, memoryview)

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "compute_signature",
    "extract_signature",
    "signed_content",
    "verify_signature",
]


def _as_body(body: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, _BODY_TYPES):
        return bytes(body)
    raise ValidationError("webhook body must be bytes, bytearray, memoryview, or str")


def _as_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise ValidationError("webhook secret must be a non-empty string")
    return secret.encode("utf-8")


def _as_timestamp(timestamp: int) -> int:
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ValidationError("webhook timestamp must be a non-negative integer")
    return timestamp


def signed_content(timestamp: int, body: bytes | bytearray | memoryview | str) -> bytes:
    """Return the exact bytes that are signed."""
    return str(_as_timestamp(timestamp)).encode("ascii") + b"." + _as_body(body)


def compute_signature(
    secret: str,
    body: bytes | bytearray | memoryview | str,
    timestamp: int,
) -> str:
    """Return the ``v1=<hex>`` signature for a delivery."""
    digest = hmac.new(
        _as_secret(secret),
        signed_content(timestamp, body),
        hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_signature(
    secret: str,
    body: bytes | bytearray | memoryview | str,
    signature: str,
    timestamp: int,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: int | None = None,
) -> bool:
    """Verify a delivery signature and its replay window.

    Returns ``False`` for a malformed signature, a mismatch, or a timestamp
    outside ``tolerance_seconds``. Raises :class:`ValidationError` when the
    arguments themselves are invalid (a programming error).
    """
    _as_secret(secret)
    _as_timestamp(timestamp)
    if isinstance(tolerance_seconds, bool) or not isinstance(tolerance_seconds, int):
        raise ValidationError("tolerance_seconds must be an integer")
    if tolerance_seconds < 0:
        raise ValidationError("tolerance_seconds must be non-negative")
    if not isinstance(signature, str):
        return False
    current = int(time.time()) if now is None else _as_timestamp(now)
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = compute_signature(secret, body, timestamp)
    return hmac.compare_digest(expected, signature)


def extract_signature(headers: Mapping[str, str]) -> tuple[str | None, int | None]:
    """Read ``(signature, timestamp)`` from headers, case-insensitively."""
    signature = None
    raw_timestamp = None
    for key, value in headers.items():
        lowered = str(key).lower()
        if lowered == SIGNATURE_HEADER.lower():
            signature = str(value)
        elif lowered == TIMESTAMP_HEADER.lower():
            raw_timestamp = str(value)
    if signature is None or raw_timestamp is None:
        return None, None
    try:
        timestamp = int(raw_timestamp)
    except ValueError:
        return None, None
    if timestamp < 0:
        return None, None
    return signature, timestamp
