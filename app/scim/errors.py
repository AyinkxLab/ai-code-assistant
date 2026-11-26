"""RFC 7644 SCIM error responses (#592).

SCIM clients expect the ``urn:ietf:params:scim:api:messages:2.0:Error`` schema
and a *string* ``status``. These helpers are deliberately not called
``error_response`` so the public catalog's AST guard does not treat SCIM payloads
as catalog responses; the two error formats stay independent.
"""

from __future__ import annotations

from flask import jsonify

SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

SCIM_TYPE_INVALID_FILTER = "invalidFilter"
SCIM_TYPE_INVALID_VALUE = "invalidValue"
SCIM_TYPE_INVALID_SYNTAX = "invalidSyntax"
SCIM_TYPE_UNIQUENESS = "uniqueness"
SCIM_TYPE_MUTABILITY = "mutability"


class ScimError(Exception):
    """A SCIM protocol error with a fixed status, type, and safe detail."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


def scim_error(status: int, detail: str, scim_type: str | None = None):
    """Build a SCIM error response; ``detail`` must be a safe, bounded string."""
    body = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "detail": detail,
        "status": str(status),
    }
    if scim_type is not None:
        body["scimType"] = scim_type
    return jsonify(body), status


__all__ = [
    "SCIM_ERROR_SCHEMA",
    "SCIM_TYPE_INVALID_FILTER",
    "SCIM_TYPE_INVALID_SYNTAX",
    "SCIM_TYPE_INVALID_VALUE",
    "SCIM_TYPE_MUTABILITY",
    "SCIM_TYPE_UNIQUENESS",
    "ScimError",
    "scim_error",
]
