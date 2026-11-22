"""Data classification labels for stored records, exports, and logs (#612).

Every stored field the application serializes falls into one of four levels:

* ``public`` — safe for an unauthenticated/public audience (ids, action names).
* ``internal`` — safe for authenticated workspace members (names, timestamps).
* ``confidential`` — workspace content (messages, file bodies, metadata blobs).
* ``pii`` — personal data and secrets (email, username, tokens, key material).

The mapping is deny-by-default for unknown fields: they are ``internal``, so a
public export never includes a field merely because nobody classified it.
Callers exporting to an audience narrower than the data's classification must
pass the maximum allowed level; :func:`export_record` and
:func:`redact_for_log` enforce it.
"""

from __future__ import annotations

PUBLIC = "public"
INTERNAL = "internal"
CONFIDENTIAL = "confidential"
PII = "pii"

LEVELS = (PUBLIC, INTERNAL, CONFIDENTIAL, PII)
RANK = {level: index for index, level in enumerate(LEVELS)}
DEFAULT_LEVEL = INTERNAL

#: Field-name to classification. Keys are matched case-insensitively.
FIELD_LEVELS: dict[str, str] = {
    # Public identifiers and action names.
    "id": PUBLIC,
    "workspace_id": PUBLIC,
    "user_id": PUBLIC,
    "actor_id": PUBLIC,
    "target_id": PUBLIC,
    "target_type": PUBLIC,
    "action": PUBLIC,
    "slug": PUBLIC,
    "classification": PUBLIC,
    "level": PUBLIC,
    # Internal operational facts.
    "name": INTERNAL,
    "title": INTERNAL,
    "status": INTERNAL,
    "role": INTERNAL,
    "provider": INTERNAL,
    "model": INTERNAL,
    "source": INTERNAL,
    "language": INTERNAL,
    "size": INTERNAL,
    "created_at": INTERNAL,
    "updated_at": INTERNAL,
    "installed_at": INTERNAL,
    "connected_at": INTERNAL,
    "retention_days": INTERNAL,
    "legal_hold": INTERNAL,
    # Workspace content.
    "content": CONFIDENTIAL,
    "body": CONFIDENTIAL,
    "message": CONFIDENTIAL,
    "text": CONFIDENTIAL,
    "prompt": CONFIDENTIAL,
    "completion": CONFIDENTIAL,
    "metadata": CONFIDENTIAL,
    "meta": CONFIDENTIAL,
    "event_metadata": CONFIDENTIAL,
    "config": CONFIDENTIAL,
    "configuration": CONFIDENTIAL,
    "manifest": CONFIDENTIAL,
    "dependencies": CONFIDENTIAL,
    "encrypted_value": CONFIDENTIAL,
    # Personal data and secret material.
    "email": PII,
    "username": PII,
    "display_name": PII,
    "password": PII,
    "password_hash": PII,
    "token": PII,
    "token_hash": PII,
    "access_token": PII,
    "refresh_token": PII,
    "api_key": PII,
    "secret": PII,
}


def validate_level(level: str) -> str:
    """Return ``level`` when it is a known classification, else raise ValueError."""
    normalized = (level or "").strip().lower()
    if normalized not in RANK:
        raise ValueError(f"Unknown classification: {level!r}.")
    return normalized


def level_for(field: str, default: str = DEFAULT_LEVEL) -> str:
    """Return the classification of ``field`` (unknown names are internal)."""
    return FIELD_LEVELS.get((field or "").lower(), default)


def allows(max_level: str, level: str) -> bool:
    """Return ``True`` when data at ``level`` may appear in a ``max_level`` export."""
    return RANK[validate_level(level)] <= RANK[validate_level(max_level)]


def export_record(record: dict, *, max_level: str = INTERNAL) -> dict:
    """Return only fields whose classification is at or below ``max_level``."""
    validate_level(max_level)
    return {key: value for key, value in record.items() if allows(max_level, level_for(key))}


def public_export(record: dict) -> dict:
    """Project a record for a public audience (``public`` fields only)."""
    return export_record(record, max_level=PUBLIC)


def public_export_rows(rows) -> list[dict]:
    """Project each record for a public audience."""
    return [public_export(row) for row in rows]


def redact_for_log(fields: dict, *, max_level: str = INTERNAL) -> dict:
    """Replace confidential/PII values with a bounded redaction marker.

    Used by structured logging so a caller cannot accidentally emit workspace
    content or personal data in a log line, even under a generically named key.
    """
    validate_level(max_level)
    redacted = {}
    for key, value in fields.items():
        level = level_for(key)
        redacted[key] = value if allows(max_level, level) else f"[redacted:{level}]"
    return redacted


__all__ = [
    "CONFIDENTIAL",
    "DEFAULT_LEVEL",
    "FIELD_LEVELS",
    "INTERNAL",
    "LEVELS",
    "PII",
    "PUBLIC",
    "RANK",
    "allows",
    "export_record",
    "level_for",
    "public_export",
    "public_export_rows",
    "redact_for_log",
    "validate_level",
]
