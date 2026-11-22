"""Bounded compliance evidence pack (#611).

Builds a snapshot of the controls and current configuration an auditor would
ask for: recent audit export, access review, retention policy, and security
settings. The pack is **evidence only** — it is not a certification,
attestation, or audit opinion, and the payload states that explicitly so it is
never mistaken for one.

Every collection is bounded: audit events and access rows are capped, and a
``truncated`` flag records whether more rows exist. Personal data is excluded by
projecting records through :mod:`app.services.classification` at the
``internal`` level (no usernames, emails, metadata blobs, or tokens).
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models import AuditLog, Workspace, WorkspaceMember
from app.services.audit import verify_chain
from app.services.audit_retention import policy_snapshot
from app.services.classification import INTERNAL, export_record
from app.services.permissions import can

SCHEMA_VERSION = 1
MAX_AUDIT_EVENTS = 50
MAX_ACCESS_ROWS = 200

DISCLAIMER = (
    "This pack is evidence of current configuration and recent activity. "
    "It is not a certification, attestation, or audit opinion."
)


def _security_settings() -> dict:
    """Return non-secret security switches from the running configuration."""
    config = current_app.config
    return {
        "session_cookie_secure": bool(config.get("SESSION_COOKIE_SECURE", False)),
        "session_cookie_httponly": bool(config.get("SESSION_COOKIE_HTTPONLY", True)),
        "session_cookie_samesite": config.get("SESSION_COOKIE_SAMESITE", "Lax"),
        "csrf_enabled": bool(config.get("WTF_CSRF_ENABLED", True)),
        "chat_audit_logging": bool(config.get("CHAT_AUDIT_LOG_ENABLED", True)),
        "metrics_enabled": bool(config.get("METRICS_ENABLED", True)),
        "security_headers": True,
    }


def build_evidence_pack(
    workspace_id: int,
    *,
    actor=None,
    now: datetime | None = None,
) -> dict:
    """Build the bounded evidence pack for ``workspace_id``.

    Raises ``LookupError`` when the workspace does not exist and
    ``PermissionError`` when ``actor`` is supplied without the owner-level
    ``view_audit`` capability. The CLI/operator path omits ``actor`` and relies
    on deployment credentials.
    """
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise LookupError("Workspace not found.")
    if actor is not None and not can("view_audit", workspace_id, actor):
        raise PermissionError("You do not have permission to read the audit log.")

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    audit_query = AuditLog.query.filter(AuditLog.workspace_id == workspace_id)
    audit_total = audit_query.count()
    audit_rows = audit_query.order_by(AuditLog.id.desc()).limit(MAX_AUDIT_EVENTS + 1).all()
    audit_truncated = len(audit_rows) > MAX_AUDIT_EVENTS
    audit_rows = audit_rows[:MAX_AUDIT_EVENTS]
    verification = verify_chain(workspace_id)

    members_query = WorkspaceMember.query.filter_by(workspace_id=workspace_id)
    member_total = members_query.count()
    member_rows = members_query.order_by(WorkspaceMember.id.asc()).limit(MAX_ACCESS_ROWS + 1).all()
    members_truncated = len(member_rows) > MAX_ACCESS_ROWS
    member_rows = member_rows[:MAX_ACCESS_ROWS]
    role_counts = {
        role: int(count)
        for role, count in db.session.query(WorkspaceMember.role, func.count())
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .group_by(WorkspaceMember.role)
        .all()
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "compliance-evidence-pack",
        "disclaimer": DISCLAIMER,
        "certification": False,
        "generated_at": moment.isoformat(),
        "workspace": {"id": workspace.id, "name": workspace.name},
        "limits": {
            "audit_events": MAX_AUDIT_EVENTS,
            "access_rows": MAX_ACCESS_ROWS,
        },
        "controls": [
            {
                "id": "audit_export",
                "name": "Audit trail export and integrity",
                "status": "implemented",
                "evidence": {
                    "total_events": audit_total,
                    "included_events": len(audit_rows),
                    "truncated": audit_truncated,
                    "chain_ok": verification["ok"],
                    "chain_checked": verification["checked"],
                    "events": [
                        export_record(row.to_dict(), max_level=INTERNAL) for row in audit_rows
                    ],
                },
            },
            {
                "id": "access_review",
                "name": "Workspace access review",
                "status": "implemented",
                "evidence": {
                    "total_members": member_total,
                    "included_members": len(member_rows),
                    "truncated": members_truncated,
                    "role_counts": role_counts,
                    "members": [
                        export_record(member.to_dict(), max_level=INTERNAL)
                        for member in member_rows
                    ],
                },
            },
            {
                "id": "retention",
                "name": "Audit retention and legal hold",
                "status": "implemented",
                "evidence": policy_snapshot(workspace_id),
            },
            {
                "id": "security_settings",
                "name": "Security configuration",
                "status": "implemented",
                "evidence": _security_settings(),
            },
        ],
    }


__all__ = [
    "DISCLAIMER",
    "MAX_ACCESS_ROWS",
    "MAX_AUDIT_EVENTS",
    "SCHEMA_VERSION",
    "build_evidence_pack",
]
