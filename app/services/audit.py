"""Audit logging for sensitive actions (issue #34, #606).

Call :func:`record` from a route right before committing; it only ever appends
a new :class:`~app.models.audit_log.AuditLog` row. Existing rows are never
updated or removed, which keeps the trail append-only.

Each append also extends a per-workspace SHA-256 hash chain: the row stores the
previous row's hash (or :data:`GENESIS_HASH` for the first hashed row) and its
own hash. :func:`verify_chain` recomputes the chain so a modified or missing
row can be reported by ``flask audit verify``.

The hash deliberately covers only immutable facts — workspace, action, target,
metadata, timestamp and the previous hash. ``user_id`` is excluded because
deleting a user nulls it (``ondelete="SET NULL"``); including it would report
every historical row as tampered after a legitimate account deletion.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.extensions import db
from app.models.audit_log import AuditLog

#: Action names used across the app. Kept as constants so filters and tests
#: never rely on stringly-typed literals.
LOGIN_SUCCESS = "login_success"
LOGIN_FAILURE = "login_failure"
USER_CREATED = "user_created"
CONVERSATION_DELETED = "conversation_deleted"
PROMPT_DELETED = "prompt_deleted"
PROVIDER_BILLING_IMPORTED = "provider_billing_imported"
BUDGET_CREATED = "budget_created"
BUDGET_UPDATED = "budget_updated"
BUDGET_DELETED = "budget_deleted"
BUDGET_OVERRIDE_CREATED = "budget_override_created"
AUDIT_EXPORTED = "audit_exported"
DATA_EXPORT_REQUESTED = "data_export_requested"
DATA_EXPORT_READY = "data_export_ready"
DATA_EXPORT_DOWNLOADED = "data_export_downloaded"
DATA_EXPORT_EXPIRED = "data_export_expired"
USER_DELETION_REQUESTED = "user_deletion_requested"
USER_DELETION_CONFIRMED = "user_deletion_confirmed"
USER_DELETION_CANCELLED = "user_deletion_cancelled"
USER_DELETION_COMPLETED = "user_deletion_completed"
SIEM_ENDPOINT_CONFIGURED = "siem_endpoint_configured"
SIEM_ENDPOINT_DELETED = "siem_endpoint_deleted"
DOMAIN_CHALLENGE_CREATED = "domain_challenge_created"
DOMAIN_VERIFIED = "domain_verified"
DOMAIN_SSO_ENFORCEMENT_CHANGED = "domain_sso_enforcement_changed"
IP_ALLOWLIST_UPDATED = "ip_allowlist_updated"
LEGAL_HOLD_PLACED = "legal_hold_placed"
LEGAL_HOLD_RELEASED = "legal_hold_released"
API_TOKEN_CREATED = "api_token_created"
API_TOKEN_ROTATED = "api_token_rotated"
API_TOKEN_REVOKED = "api_token_revoked"
SESSIONS_REVOKED = "sessions_revoked"
PLUGIN_POLICY_UPDATED = "plugin_policy_updated"
PLUGIN_INSTALL_BLOCKED = "plugin_install_blocked"
PLUGIN_INSTALL_APPROVED = "plugin_install_approved"
ORG_ROLE_CREATED = "organization_role_created"
ORG_ROLE_UPDATED = "organization_role_updated"
ORG_ROLE_DELETED = "organization_role_deleted"

#: The "previous hash" of the first hashed row in a chain.
GENESIS_HASH = "0" * 64

#: Per-record verification findings.
PROBLEM_MODIFIED = "modified"
PROBLEM_MISSING = "missing_or_reordered"


def _normalize_moment(value: datetime | None) -> datetime | None:
    """Treat naive timestamps (SQLite) as UTC so recomputation is stable."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonical_payload(entry: AuditLog, prev_hash: str | None) -> str:
    """Serialize the hashed fields deterministically."""
    moment = _normalize_moment(entry.created_at)
    payload = {
        "workspace_id": entry.workspace_id,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "metadata": entry.meta,
        "created_at": moment.isoformat() if moment else None,
        "prev_hash": prev_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_record_hash(entry: AuditLog, prev_hash: str | None) -> str:
    """Return the SHA-256 record hash for ``entry`` linked to ``prev_hash``."""
    return hashlib.sha256(canonical_payload(entry, prev_hash).encode("utf-8")).hexdigest()


def _chain_query(workspace_id: int | None):
    query = AuditLog.query
    if workspace_id is None:
        return query.filter(AuditLog.workspace_id.is_(None))
    return query.filter(AuditLog.workspace_id == workspace_id)


def _previous_entry(workspace_id: int | None) -> AuditLog | None:
    # shortcut: appends are serialized with FOR UPDATE on PostgreSQL; SQLite
    # ignores it, so concurrent writers on SQLite can race. Upgrade to an
    # advisory lock or a sequence table if SQLite becomes a shared writer.
    return (
        _chain_query(workspace_id).order_by(AuditLog.id.desc()).limit(1).with_for_update().first()
    )


def record(
    action: str,
    *,
    workspace_id: int | None = None,
    user=None,
    user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    metadata: dict | None = None,
) -> AuditLog:
    """Append an audit entry to the session (does not commit).

    ``user`` may be a ``User`` instance (its id is used) or ``None`` for events
    with no authenticated actor (e.g. a failed login), in which case ``user_id``
    can still be supplied explicitly. ``workspace_id`` scopes the hash chain;
    entries with no workspace share the ``NULL`` chain. Contextual facts belong
    in ``metadata``.
    """
    entry = AuditLog(
        workspace_id=workspace_id,
        user_id=user.id if user is not None else user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        meta=json.dumps(metadata) if metadata else None,
        created_at=datetime.now(UTC),
    )
    previous = _previous_entry(workspace_id)
    entry.prev_hash = (
        previous.record_hash if previous is not None and previous.record_hash else GENESIS_HASH
    )
    entry.record_hash = compute_record_hash(entry, entry.prev_hash)
    db.session.add(entry)
    return entry


def verify_chain(workspace_id: int | None = None) -> dict:
    """Recompute one chain and report modified or missing rows.

    The first hashed row is treated as an anchor: retention purges remove a
    contiguous prefix, so the earliest surviving row is expected to reference a
    row that no longer exists. Every later row must link to its immediate
    predecessor and its hash must match. Legacy rows with ``NULL`` hashes are
    counted as ``unhashed`` and skipped.
    """
    errors: list[dict] = []
    checked = 0
    unhashed = 0
    previous: AuditLog | None = None

    for row in _chain_query(workspace_id).order_by(AuditLog.id.asc()).all():
        if not row.record_hash:
            unhashed += 1
            previous = None
            continue
        if previous is not None and row.prev_hash != previous.record_hash:
            errors.append({"id": row.id, "problem": PROBLEM_MISSING})
        if compute_record_hash(row, row.prev_hash) != row.record_hash:
            errors.append({"id": row.id, "problem": PROBLEM_MODIFIED})
        checked += 1
        previous = row

    return {
        "workspace_id": workspace_id,
        "ok": not errors,
        "checked": checked,
        "unhashed": unhashed,
        "errors": errors,
    }


def verify_all_chains() -> list[dict]:
    """Verify every distinct workspace chain (including the ``NULL`` chain)."""
    ids = [row[0] for row in db.session.query(AuditLog.workspace_id).distinct().all()]
    ids.sort(key=lambda value: (value is not None, value or 0))
    return [verify_chain(workspace_id) for workspace_id in ids]
