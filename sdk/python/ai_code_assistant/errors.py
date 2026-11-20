"""Typed errors for the AI Code Assistant Python SDK.

The mapping in this module is kept in sync with
``sdk/fixtures/error-mapping.json`` (the shared contract used by the Python
and TypeScript SDKs) and is asserted by the test suite.

Every raised error preserves the raw server ``code`` and the request id so a
support ticket can be correlated with the server-side request log.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "CODE_TO_ERROR",
    "IDEMPOTENT_METHODS",
    "REQUEST_ID_HEADER",
    "RETRYABLE_STATUSES",
    "STATUS_TO_ERROR",
    "APITimeoutError",
    "AuthenticationError",
    "ConflictError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "SDKError",
    "ServerError",
    "TransportError",
    "ValidationError",
    "error_from_response",
]

#: Header used by the API to echo the request correlation id.
REQUEST_ID_HEADER = "X-Request-ID"

#: Methods that are safe to retry: they do not create a new resource.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: Only retry status codes that are transient and safe to repeat.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

STATUS_TO_ERROR: dict[int, str] = {
    400: "ValidationError",
    401: "AuthenticationError",
    403: "PermissionDeniedError",
    404: "NotFoundError",
    409: "ConflictError",
    422: "ValidationError",
    429: "RateLimitError",
    500: "ServerError",
    502: "ServerError",
    503: "ServerError",
    504: "ServerError",
}

CODE_TO_ERROR: dict[str, str] = {
    "invalid_request": "ValidationError",
    "invalid_input": "ValidationError",
    "validation_error": "ValidationError",
    "unauthorized": "AuthenticationError",
    "invalid_token": "AuthenticationError",
    "authentication_required": "AuthenticationError",
    "forbidden": "PermissionDeniedError",
    "insufficient_scope": "PermissionDeniedError",
    "not_found": "NotFoundError",
    "conflict": "ConflictError",
    "rate_limited": "RateLimitError",
    "internal_error": "ServerError",
    "server_error": "ServerError",
}


class SDKError(Exception):
    """Base class for every error raised by the SDK."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.payload = payload

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"(code={self.code})")
        if self.status_code is not None:
            parts.append(f"[status={self.status_code}]")
        if self.request_id:
            parts.append(f"[request_id={self.request_id}]")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of the error."""
        return {
            "type": type(self).__name__,
            "message": self.message,
            "status_code": self.status_code,
            "code": self.code,
            "request_id": self.request_id,
            "payload": self.payload,
        }


class ValidationError(SDKError):
    """The request was rejected as invalid (HTTP 400/422)."""


class AuthenticationError(SDKError):
    """The API token is missing or invalid (HTTP 401)."""


class PermissionDeniedError(SDKError):
    """The token is valid but lacks permission for the resource (HTTP 403)."""


class NotFoundError(SDKError):
    """The requested resource does not exist (HTTP 404)."""


class ConflictError(SDKError):
    """The request conflicts with the current resource state (HTTP 409)."""


class RateLimitError(SDKError):
    """The request was rate limited (HTTP 429)."""


class ServerError(SDKError):
    """The server failed to process the request (HTTP 5xx)."""


class TransportError(SDKError):
    """The request could not be completed at the transport layer."""


class APITimeoutError(TransportError):
    """The request exceeded the configured timeout."""


_ERROR_CLASSES: dict[str, type[SDKError]] = {
    "ValidationError": ValidationError,
    "AuthenticationError": AuthenticationError,
    "PermissionDeniedError": PermissionDeniedError,
    "NotFoundError": NotFoundError,
    "ConflictError": ConflictError,
    "RateLimitError": RateLimitError,
    "ServerError": ServerError,
    "TransportError": TransportError,
    "APITimeoutError": APITimeoutError,
}


def _extract_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            for key in ("message", "detail", "description"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "detail", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:500]
    return f"API request failed with status {status_code}"


def _extract_code(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    code = payload.get("code")
    if isinstance(code, str) and code.strip():
        return code.strip()
    return None


def error_from_response(
    status_code: int,
    payload: Any,
    *,
    request_id: str | None = None,
) -> SDKError:
    """Map an HTTP response to a typed :class:`SDKError` subclass.

    The server ``code`` wins over the HTTP status so that a stable API code can
    be mapped even when a proxy rewrites the status. Unknown statuses fall back
    to :class:`ServerError` for 5xx and the base :class:`SDKError` otherwise.
    """
    code = _extract_code(payload)
    type_name = CODE_TO_ERROR.get(code or "") or STATUS_TO_ERROR.get(status_code)
    if type_name is None:
        type_name = "ServerError" if status_code >= 500 else "SDKError"
    error_class = _ERROR_CLASSES.get(type_name, SDKError)
    return error_class(
        _extract_message(payload, status_code),
        status_code=status_code,
        code=code,
        request_id=request_id,
        payload=payload,
    )
