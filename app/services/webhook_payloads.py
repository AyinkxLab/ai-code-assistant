"""Bounded webhook payload preparation (#674).

Outbound webhook bodies are capped by ``WEBHOOK_MAX_PAYLOAD_BYTES``. The
default policy is **reject**: an oversized payload raises a structured
:class:`WebhookPayloadTooLarge` error instead of being silently changed.

A caller that owns the event schema may explicitly opt in to truncation by
naming the optional fields that may be shortened. Required fields are never
truncated: naming a required field in the truncation allowlist is a
programming error and raises :class:`WebhookPayloadError`. Truncation is
reported back to the caller so it can be logged or surfaced, rather than
happening invisibly.

The size is measured on the exact UTF-8 bytes that are sent on the wire:
``json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from flask import current_app

DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = DEFAULT_MAX_PAYLOAD_BYTES

#: Error code returned by API callers for an oversized payload.
PAYLOAD_TOO_LARGE = "webhook_payload_too_large"
INVALID_PAYLOAD = "invalid_webhook_payload"


class WebhookPayloadError(ValueError):
    """Base class for invalid or oversized webhook payloads."""

    code = INVALID_PAYLOAD
    http_status = 400


class WebhookPayloadTooLarge(WebhookPayloadError):
    """The serialized payload exceeds the configured byte cap."""

    code = PAYLOAD_TOO_LARGE
    http_status = 413

    def __init__(self, message: str, *, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        self.limit = limit


def configured_limit() -> int:
    """Return the configured maximum payload size in bytes.

    A malformed or non-positive setting falls back to the safe default rather
    than disabling the bound.
    """
    try:
        value = int(current_app.config.get("WEBHOOK_MAX_PAYLOAD_BYTES", MAX_PAYLOAD_BYTES))
    except (RuntimeError, TypeError, ValueError):
        return MAX_PAYLOAD_BYTES
    return value if value > 0 else MAX_PAYLOAD_BYTES


def serialize_payload(payload: dict) -> bytes:
    """Return the exact UTF-8 bytes used for signing and delivery."""
    if not isinstance(payload, dict):
        raise WebhookPayloadError("webhook payload must be a JSON object")
    try:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise WebhookPayloadError("webhook payload is not JSON-serializable") from exc
    return encoded.encode("utf-8")


def payload_size(payload: dict) -> int:
    """Return the serialized size of ``payload`` in bytes."""
    return len(serialize_payload(payload))


def _shrink(value, remaining: int):
    """Shrink a string or list so its serialized form is not larger than needed.

    ``remaining`` is the number of characters still available for this field.
    Only strings and lists of strings are shortened; any other type is left
    untouched and the final size check decides whether truncation succeeded.
    """
    if isinstance(value, str):
        return value[: max(0, remaining)]
    if isinstance(value, list):
        kept: list = []
        used = 0
        for item in value:
            if not isinstance(item, str):
                kept.append(item)
                continue
            if used >= remaining:
                break
            chunk = item[: remaining - used]
            kept.append(chunk)
            used += len(chunk)
        return kept
    return value


def prepare_payload(
    payload: dict,
    *,
    max_bytes: int | None = None,
    truncate_fields: Iterable[str] = (),
    required_fields: Iterable[str] = (),
) -> tuple[dict, tuple[str, ...]]:
    """Return ``(payload, truncated_fields)`` within the configured byte cap.

    With no ``truncate_fields`` an oversized payload is rejected. Truncation is
    limited to the named optional fields; a required field is never shortened.
    If truncating every allowed field still does not fit, the payload is
    rejected rather than silently dropping data.
    """
    limit = configured_limit() if max_bytes is None else int(max_bytes)
    if limit <= 0:
        raise WebhookPayloadError("max_bytes must be a positive integer")
    required = {str(field) for field in required_fields}
    allowed = tuple(dict.fromkeys(str(field) for field in truncate_fields))
    overlap = required.intersection(allowed)
    if overlap:
        raise WebhookPayloadError(
            "required webhook fields cannot be truncated: " + ", ".join(sorted(overlap))
        )

    size = payload_size(payload)
    if size <= limit:
        return payload, ()

    if not allowed:
        raise WebhookPayloadTooLarge(
            f"webhook payload is {size} bytes, exceeding the {limit}-byte limit",
            size=size,
            limit=limit,
        )

    import copy

    result = copy.deepcopy(payload)
    truncated: list[str] = []
    # Greedily shorten each allowed field. The exact serialized overhead is
    # accounted for by re-measuring after every field, so the loop always
    # converges on a payload that is at or below the cap when possible.
    for field in allowed:
        if field not in result:
            continue
        overshoot = payload_size(result) - limit
        if overshoot <= 0:
            break
        before = result[field]
        # Leave a small margin for JSON quoting/escaping around the field.
        reduced = _shrink(before, max(0, len(str(before)) - overshoot - 16))
        if reduced == before:
            continue
        result[field] = reduced
        truncated.append(field)

    final_size = payload_size(result)
    if final_size > limit:
        raise WebhookPayloadTooLarge(
            f"webhook payload is {final_size} bytes even after truncating "
            f"{', '.join(truncated) or '<no fields>'}; limit is {limit} bytes",
            size=final_size,
            limit=limit,
        )
    return result, tuple(truncated)


__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "INVALID_PAYLOAD",
    "MAX_PAYLOAD_BYTES",
    "PAYLOAD_TOO_LARGE",
    "WebhookPayloadError",
    "WebhookPayloadTooLarge",
    "configured_limit",
    "payload_size",
    "prepare_payload",
    "serialize_payload",
]
