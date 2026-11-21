"""HMAC-SHA256 signing and verification for outbound webhooks.

Deliveries are signed over the exact bytes sent on the wire. The signed string
is::

    <timestamp>.<raw body bytes>

where ``<timestamp>`` is the Unix time in seconds as ASCII digits and the
separator is a single ``.``. The signature header is::

    X-AICA-Signature: v1=<lowercase hex HMAC-SHA256>

and ``X-AICA-Timestamp`` carries the same timestamp used in the signature. A
verifier must:

1. parse the timestamp and reject it when it is outside the tolerance window
   (replay protection);
2. recompute the signature over ``"<timestamp>." + raw_body`` using the shared
   secret;
3. compare the versioned signatures in constant time.

The secret is the per-subscription signing secret. It is never written to logs
or included in the payload.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

#: Header carrying the versioned signature.
SIGNATURE_HEADER = "X-AICA-Signature"

#: Header carrying the Unix timestamp used to build the signature.
TIMESTAMP_HEADER = "X-AICA-Timestamp"

#: Current signature version prefix.
SIGNATURE_VERSION = "v1"

#: Default maximum age (and future skew) accepted by :func:`verify_signature`.
DEFAULT_TOLERANCE_SECONDS = 300

_VALID_BODY_TYPES = (bytes, bytearray, memoryview)


def _as_body(body: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, _VALID_BODY_TYPES):
        return bytes(body)
    raise TypeError("webhook body must be bytes, bytearray, memoryview, or str")


def _as_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise ValueError("webhook secret must be a non-empty string")
    return secret.encode("utf-8")


def _as_timestamp(timestamp: int) -> int:
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ValueError("webhook timestamp must be a non-negative integer")
    return timestamp


def signed_content(timestamp: int, body: bytes | bytearray | memoryview | str) -> bytes:
    """Return the exact byte string that is signed for ``timestamp`` and ``body``."""
    timestamp = _as_timestamp(timestamp)
    return str(timestamp).encode("ascii") + b"." + _as_body(body)


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


def signature_headers(
    secret: str,
    body: bytes | bytearray | memoryview | str,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Return the two signing headers for a delivery.

    ``timestamp`` defaults to the current Unix time. Tests pass it explicitly so
    the output is deterministic.
    """
    if timestamp is None:
        timestamp = int(time.time())
    return {
        SIGNATURE_HEADER: compute_signature(secret, body, timestamp),
        TIMESTAMP_HEADER: str(timestamp),
    }


def verify_signature(
    secret: str,
    body: bytes | bytearray | memoryview | str,
    signature: str,
    timestamp: int,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: int | None = None,
) -> bool:
    """Return whether ``signature`` is valid and fresh.

    Invalid signature syntax, a mismatched signature, and a timestamp outside
    the tolerance window all return ``False``. Programmer errors (bad types or a
    non-string secret) raise ``ValueError``/``TypeError`` so misconfiguration is
    not silently treated as an invalid signature.
    """
    _as_secret(secret)
    _as_timestamp(timestamp)
    if isinstance(tolerance_seconds, bool) or not isinstance(tolerance_seconds, int):
        raise ValueError("tolerance_seconds must be an integer")
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be non-negative")
    if not isinstance(signature, str):
        return False
    current = int(time.time()) if now is None else _as_timestamp(now)
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = compute_signature(secret, body, timestamp)
    return hmac.compare_digest(expected, signature)


def extract_signature(headers: Mapping[str, str]) -> tuple[str | None, int | None]:
    """Extract ``(signature, timestamp)`` from request headers.

    Header names are matched case-insensitively. Returns ``(None, None)`` when a
    header is missing or the timestamp is not an integer so a caller can reject
    the delivery without raising.
    """
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


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "compute_signature",
    "extract_signature",
    "signature_headers",
    "signed_content",
    "verify_signature",
]
