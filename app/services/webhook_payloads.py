"""Webhook payload size limit and validation service (Phase 12, #674).

Enforces deterministic payload bounds and handling policies for webhooks:
- Rejection policy (default): Oversized payloads fail closed with HTTP 413
  and structured error details.
- Truncation policy: Non-essential voluminous fields (such as diffs, patches,
  or raw logs) are safely truncated with explicit metadata; required event
  identity and envelope fields are NEVER silently truncated.
- Pre-buffering protection: Evaluates ``Content-Length`` headers and reads
  streams in bounded chunks, preventing memory exhaustion before reading
  oversized requests into memory.
- Privacy guarantee: Payloads, credentials, and authorization headers are
  never leaked into server logs or unhandled exception traces.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)

#: Default maximum webhook payload size in bytes (256 KB).
DEFAULT_WEBHOOK_MAX_PAYLOAD_BYTES = 256 * 1024

#: Default policy for handling payloads exceeding the size limit ("reject" or "truncate").
DEFAULT_OVERSIZED_POLICY = "reject"
ALLOWED_OVERSIZED_POLICIES = frozenset({"reject", "truncate"})

#: Structural / envelope fields that must never be silently truncated.
REQUIRED_IMMUTABLE_FIELDS = frozenset(
    {
        "id",
        "event",
        "event_type",
        "timestamp",
        "workspace_id",
        "user_id",
        "version",
        "delivery_id",
        "signature",
    }
)

#: Known non-essential fields eligible for safe truncation under the truncate policy.
TRUNCATABLE_FIELD_NAMES = ("diff", "raw", "content", "output", "patch", "body", "findings", "items")


@dataclass(frozen=True)
class WebhookLimits:
    """Resolved webhook limits and policies for the active context."""

    max_payload_bytes: int
    policy: str


@dataclass
class ValidationResult:
    """Outcome of validating and parsing a webhook payload."""

    is_valid: bool
    payload: dict[str, Any] | None = None
    raw_bytes: bytes | None = None
    size: int = 0
    status_code: int = 200
    error: str | None = None
    code: str | None = None
    truncated: bool = False
    truncated_fields: list[str] = field(default_factory=list)

    def to_error_response(self) -> dict[str, Any]:
        """Return standardized structured error response dictionary."""
        response: dict[str, Any] = {
            "error": self.error or "Webhook request failed validation.",
            "code": self.code or "invalid_request",
        }
        if self.code == "payload_too_large":
            response["limit_bytes"] = self.size
        return response


class WebhookError(Exception):
    """Base exception for webhook processing errors."""

    def __init__(self, message: str, code: str = "webhook_error", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class WebhookPayloadTooLargeError(WebhookError):
    """Raised when a payload exceeds the configured byte ceiling."""

    def __init__(
        self,
        message: str,
        limit_bytes: int,
        actual_bytes: int,
        code: str = "payload_too_large",
    ):
        super().__init__(message, code=code, status_code=413)
        self.limit_bytes = limit_bytes
        self.actual_bytes = actual_bytes


class WebhookMalformedPayloadError(WebhookError):
    """Raised when the payload is not valid JSON."""

    def __init__(self, message: str = "Malformed JSON payload.", code: str = "malformed_payload"):
        super().__init__(message, code=code, status_code=400)


class WebhookInvalidPayloadError(WebhookError):
    """Raised when required event fields are missing or invalid."""

    def __init__(self, message: str, code: str = "invalid_payload"):
        super().__init__(message, code=code, status_code=422)


class WebhookUnauthorizedError(WebhookError):
    """Raised when webhook credentials or signatures are missing/invalid."""

    def __init__(self, message: str = "Unauthorized webhook request.", code: str = "unauthorized"):
        super().__init__(message, code=code, status_code=401)


class WebhookForbiddenError(WebhookError):
    """Raised when the webhook subscriber lacks permissions for the event."""

    def __init__(self, message: str = "Forbidden.", code: str = "forbidden"):
        super().__init__(message, code=code, status_code=403)


def get_webhook_limits() -> WebhookLimits:
    """Return effective webhook size limit and policy from current Flask app or defaults."""
    max_bytes = DEFAULT_WEBHOOK_MAX_PAYLOAD_BYTES
    policy = DEFAULT_OVERSIZED_POLICY

    try:
        from flask import current_app, has_app_context

        if has_app_context():
            configured_bytes = current_app.config.get("WEBHOOK_MAX_PAYLOAD_BYTES")
            if configured_bytes is not None:
                try:
                    parsed = int(configured_bytes)
                    if parsed > 0:
                        max_bytes = parsed
                except (TypeError, ValueError):
                    pass

            configured_policy = current_app.config.get("WEBHOOK_OVERSIZED_POLICY")
            if isinstance(configured_policy, str):
                normalized = configured_policy.strip().lower()
                if normalized in ALLOWED_OVERSIZED_POLICIES:
                    policy = normalized
    except Exception:
        pass

    return WebhookLimits(max_payload_bytes=max_bytes, policy=policy)


def read_webhook_stream(
    stream: Any,
    content_length: int | None = None,
    max_bytes: int | None = None,
    chunk_size: int = 8192,
) -> bytes:
    """Read a request body stream enforcing size bounds before full buffering.

    Args:
        stream: Byte stream (such as ``request.stream``).
        content_length: Declared ``Content-Length`` from headers, if present.
        max_bytes: Maximum allowed bytes. Defaults to configured limit.
        chunk_size: Incremental read buffer size.

    Returns:
        Full raw bytes of the request.

    Raises:
        WebhookPayloadTooLargeError: If Content-Length or stream exceeds max_bytes.
    """
    if max_bytes is None:
        max_bytes = get_webhook_limits().max_payload_bytes

    # Pre-buffering defense: fail immediately if declared size exceeds maximum
    if content_length is not None and content_length > max_bytes:
        logger.warning(
            "Rejecting webhook request: Content-Length %d exceeds limit %d bytes",
            content_length,
            max_bytes,
        )
        raise WebhookPayloadTooLargeError(
            f"Webhook payload length {content_length} bytes exceeds "
            f"maximum limit of {max_bytes} bytes.",
            limit_bytes=max_bytes,
            actual_bytes=content_length,
        )

    chunks: list[bytes] = []
    total_read = 0

    while True:
        # Request read in chunks
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_bytes:
            logger.warning(
                "Aborting webhook stream read: received %d bytes exceeding limit %d bytes",
                total_read,
                max_bytes,
            )
            raise WebhookPayloadTooLargeError(
                f"Webhook payload stream exceeded maximum limit of {max_bytes} bytes.",
                limit_bytes=max_bytes,
                actual_bytes=total_read,
            )
        chunks.append(chunk)

    return b"".join(chunks)


def enforce_payload_size(
    payload: dict[str, Any],
    max_bytes: int | None = None,
    policy: str | None = None,
) -> tuple[dict[str, Any], bool, list[str]]:
    """Enforce size limit on a dictionary payload using the specified policy.

    Args:
        payload: Event dictionary to evaluate.
        max_bytes: Maximum allowed serialized byte size.
        policy: "reject" or "truncate". Defaults to configured policy.

    Returns:
        tuple (processed_payload, was_truncated, list_of_truncated_fields)

    Raises:
        WebhookPayloadTooLargeError: If payload exceeds limit and policy is reject,
            or if truncation cannot reduce size below limit without dropping required fields.
    """
    limits = get_webhook_limits()
    if max_bytes is None:
        max_bytes = limits.max_payload_bytes
    if policy is None:
        policy = limits.policy

    # Calculate serialized UTF-8 size
    raw_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    actual_size = len(raw_json)

    if actual_size <= max_bytes:
        return payload, False, []

    if policy != "truncate":
        logger.warning(
            "Webhook payload rejected: size %d bytes exceeds maximum limit "
            "of %d bytes (policy: reject)",
            actual_size,
            max_bytes,
        )
        raise WebhookPayloadTooLargeError(
            f"Webhook payload size {actual_size} bytes exceeds maximum limit of {max_bytes} bytes.",
            limit_bytes=max_bytes,
            actual_bytes=actual_size,
        )

    # Safe Truncation Policy:
    # 1. Required fields must NEVER be truncated silently.
    # Extract immutable fields to verify they fit.
    required_data: dict[str, Any] = {
        k: v for k, v in payload.items() if k in REQUIRED_IMMUTABLE_FIELDS
    }
    required_bytes = len(
        json.dumps(required_data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )

    if required_bytes >= max_bytes:
        logger.warning(
            "Webhook payload cannot be safely truncated: required fields alone consume "
            "%d bytes >= limit %d bytes",
            required_bytes,
            max_bytes,
        )
        raise WebhookPayloadTooLargeError(
            f"Webhook required envelope fields consume {required_bytes} bytes, "
            f"exceeding limit of {max_bytes} bytes.",
            limit_bytes=max_bytes,
            actual_bytes=actual_size,
        )

    # 2. Candidate fields for truncation
    working_payload = dict(payload)
    truncated_fields: list[str] = []

    # Look inside 'data' or top-level for truncatable fields
    data_dict = working_payload.get("data")
    target_dicts: list[tuple[dict[str, Any], str]] = []
    if isinstance(data_dict, dict):
        target_dicts.append((data_dict, "data."))
    target_dicts.append((working_payload, ""))

    for container, prefix in target_dicts:
        for field_name in TRUNCATABLE_FIELD_NAMES:
            if field_name in container and isinstance(container[field_name], (str, list, dict)):
                val = container[field_name]
                orig_len = len(val)
                # Truncate string or list
                if isinstance(val, str) and orig_len > 100:
                    # Truncate string with marker
                    container[field_name] = val[:100] + "... [truncated]"
                    truncated_fields.append(f"{prefix}{field_name}")
                elif isinstance(val, list) and orig_len > 3:
                    container[field_name] = val[:3]
                    truncated_fields.append(f"{prefix}{field_name}")
                elif isinstance(val, dict) and orig_len > 3:
                    truncated_keys = list(val.keys())[:3]
                    container[field_name] = {k: val[k] for k in truncated_keys}
                    truncated_fields.append(f"{prefix}{field_name}")

                current_bytes = len(
                    json.dumps(working_payload, separators=(",", ":"), ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
                if current_bytes <= max_bytes:
                    break
        current_bytes = len(
            json.dumps(working_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        if current_bytes <= max_bytes:
            break

    # Re-check size after truncating non-essential fields
    final_bytes = len(
        json.dumps(working_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )

    if final_bytes > max_bytes:
        # If still over limit, truncate larger values further
        for container, prefix in target_dicts:
            for k in list(container.keys()):
                if (
                    k not in REQUIRED_IMMUTABLE_FIELDS
                    and isinstance(container[k], str)
                    and len(container[k]) > 30
                ):
                    container[k] = container[k][:20] + "... [truncated]"
                    if f"{prefix}{k}" not in truncated_fields:
                        truncated_fields.append(f"{prefix}{k}")
                    current_bytes = len(
                        json.dumps(
                            working_payload, separators=(",", ":"), ensure_ascii=False
                        ).encode("utf-8")
                    )
                    if current_bytes <= max_bytes:
                        break

    final_bytes = len(
        json.dumps(working_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    if final_bytes > max_bytes:
        raise WebhookPayloadTooLargeError(
            f"Webhook payload remains oversized ({final_bytes} bytes) "
            "after safe truncation attempt.",
            limit_bytes=max_bytes,
            actual_bytes=final_bytes,
        )

    # Documented truncation marker: never silent!
    working_payload["_truncated"] = True
    working_payload["_original_size"] = actual_size
    working_payload["_truncated_fields"] = truncated_fields

    logger.info(
        "Safely truncated oversized webhook payload from %d to %d bytes (fields: %s)",
        actual_size,
        final_bytes,
        ", ".join(truncated_fields),
    )
    return working_payload, True, truncated_fields


def validate_webhook_request(
    request: Any,
    max_bytes: int | None = None,
    policy: str | None = None,
    required_fields: list[str] | None = None,
) -> ValidationResult:
    """Validate and read an incoming Flask webhook request with size limit enforcement.

    Args:
        request: Flask request object.
        max_bytes: Byte limit (falls back to configured limit).
        policy: "reject" or "truncate".
        required_fields: Expected top-level keys in JSON payload.

    Returns:
        ValidationResult with parsed payload or structured error details.
    """
    limits = get_webhook_limits()
    if max_bytes is None:
        max_bytes = limits.max_payload_bytes
    if policy is None:
        policy = limits.policy

    # When policy is "truncate", allow reading up to a bounded ingestion ceiling
    # (up to 4x max_bytes or 2MB) so oversized non-essential fields can be received
    # and safely truncated down to max_bytes. Under "reject" policy, stream fails fast at max_bytes.
    stream_read_limit = max_bytes if policy != "truncate" else max(max_bytes * 4, 2 * 1024 * 1024)

    # 1. Read stream with pre-buffering check
    try:
        raw_bytes = read_webhook_stream(
            request.stream,
            content_length=request.content_length,
            max_bytes=stream_read_limit,
        )
    except WebhookPayloadTooLargeError as exc:
        return ValidationResult(
            is_valid=False,
            size=exc.actual_bytes,
            status_code=413,
            error=exc.message,
            code="payload_too_large",
        )
    except Exception:
        return ValidationResult(
            is_valid=False,
            status_code=400,
            error="Failed to read request body stream.",
            code="stream_read_error",
        )

    # 2. Check for empty body
    if not raw_bytes:
        return ValidationResult(
            is_valid=False,
            size=0,
            status_code=400,
            error="Empty webhook payload.",
            code="empty_payload",
        )

    # 3. Parse JSON
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return ValidationResult(
            is_valid=False,
            raw_bytes=raw_bytes,
            size=len(raw_bytes),
            status_code=400,
            error="Invalid JSON payload.",
            code="malformed_payload",
        )

    if not isinstance(data, dict):
        return ValidationResult(
            is_valid=False,
            raw_bytes=raw_bytes,
            size=len(raw_bytes),
            status_code=400,
            error="Webhook payload must be a JSON object.",
            code="malformed_payload",
        )

    # 4. Check required fields if specified
    if required_fields:
        for field_name in required_fields:
            if field_name not in data or data[field_name] is None:
                return ValidationResult(
                    is_valid=False,
                    payload=data,
                    raw_bytes=raw_bytes,
                    size=len(raw_bytes),
                    status_code=422,
                    error=f"Missing required payload field: '{field_name}'.",
                    code="invalid_payload",
                )

    # 5. Apply oversized policy to the payload
    try:
        processed, truncated, truncated_fields = enforce_payload_size(
            data,
            max_bytes=max_bytes,
            policy=policy,
        )
        return ValidationResult(
            is_valid=True,
            payload=processed,
            raw_bytes=raw_bytes,
            size=len(raw_bytes),
            status_code=200,
            truncated=truncated,
            truncated_fields=truncated_fields,
        )
    except WebhookPayloadTooLargeError as exc:
        return ValidationResult(
            is_valid=False,
            payload=data,
            raw_bytes=raw_bytes,
            size=exc.actual_bytes,
            status_code=413,
            error=exc.message,
            code="payload_too_large",
        )


def require_webhook_payload_limit(
    max_bytes: int | None = None,
    policy: str | None = None,
    required_fields: list[str] | None = None,
) -> Callable:
    """Flask view decorator enforcing webhook payload size limits and structured errors."""

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from flask import jsonify, request

            result = validate_webhook_request(
                request,
                max_bytes=max_bytes,
                policy=policy,
                required_fields=required_fields,
            )
            if not result.is_valid:
                return jsonify(result.to_error_response()), result.status_code

            # Pass parsed payload as kwarg 'payload' if accepted, or attach to request
            request.webhook_payload = result.payload
            request.webhook_validation = result
            return fn(*args, **kwargs)

        return wrapper

    return decorator
