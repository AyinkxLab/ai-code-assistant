"""Stable public API error catalog (#650).

Every public error a client can receive is listed once in :data:`CATALOG`.
Handlers and routes raise a catalog code instead of an ad-hoc string so that
the public contract stays stable even when internal exception text changes.
Internal details are never included: callers may override the public message
with a safe validation message, but the default is always generic.

The wire shape extends the existing ``{"error": ..., "kind": ...}`` contract
with a ``code`` field, so older clients keep working.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import jsonify, request

INVALID_INPUT = "invalid_input"
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
VALIDATION_FAILED = "validation_failed"
RATE_LIMITED = "rate_limited"
GONE = "gone"
INTERNAL_ERROR = "internal_error"
BAD_GATEWAY = "bad_gateway"


@dataclass(frozen=True)
class ApiError:
    """One stable public error entry."""

    code: str
    status: int
    kind: str
    message: str
    description: str


CATALOG: dict[str, ApiError] = {
    INVALID_INPUT: ApiError(
        INVALID_INPUT, 400, "invalid", "The request is invalid.", "Malformed or missing input."
    ),
    UNAUTHORIZED: ApiError(
        UNAUTHORIZED, 401, "unauthorized", "Authentication required.", "No valid session or key."
    ),
    FORBIDDEN: ApiError(
        FORBIDDEN, 403, "forbidden", "You do not have permission to do that.", "Denied by policy."
    ),
    NOT_FOUND: ApiError(
        NOT_FOUND, 404, "not_found", "The requested resource was not found.", "Unknown resource."
    ),
    CONFLICT: ApiError(
        CONFLICT,
        409,
        "conflict",
        "The request conflicts with the current state.",
        "State conflict.",
    ),
    VALIDATION_FAILED: ApiError(
        VALIDATION_FAILED,
        422,
        "validation_failed",
        "The request could not be validated.",
        "Schema.",
    ),
    RATE_LIMITED: ApiError(
        RATE_LIMITED, 429, "rate_limited", "Too many requests. Please try again later.", "Limit."
    ),
    GONE: ApiError(
        GONE, 410, "gone", "This resource is no longer available.", "Expired or consumed resource."
    ),
    INTERNAL_ERROR: ApiError(
        INTERNAL_ERROR, 500, "internal_error", "Something went wrong.", "Unexpected server error."
    ),
    BAD_GATEWAY: ApiError(
        BAD_GATEWAY,
        502,
        "upstream_error",
        "An upstream service failed to complete the request.",
        "A third-party delivery failed.",
    ),
}

STATUS_TO_CODE = {entry.status: entry.code for entry in CATALOG.values()}


def catalog_entry(code: str) -> ApiError:
    """Return the catalog entry for ``code`` (KeyError is a programming bug)."""
    return CATALOG[code]


def error_code_for_status(status: int) -> str:
    """Map an HTTP status to its catalog code, defaulting to ``internal_error``."""
    return STATUS_TO_CODE.get(status, INTERNAL_ERROR)


def error_response(code: str, message: str | None = None, *, details=None):
    """Build a structured JSON error response for a catalog code."""
    entry = catalog_entry(code)
    payload = {
        "error": message or entry.message,
        "kind": entry.kind,
        "code": entry.code,
    }
    if details is not None:
        payload["details"] = details
    response = jsonify(payload)
    response.status_code = entry.status
    return response


def wants_json_error() -> bool:
    """Return ``True`` when an error should use the JSON catalog shape."""
    if request.path == "/api" or request.path.startswith("/api/") or "/api/" in request.path:
        return True
    return request.accept_mimetypes.best == "application/json"
