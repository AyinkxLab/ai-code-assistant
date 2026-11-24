"""Audit retention and purge (#608).

Workspaces can set a validated retention window and a legal hold. The purge
removes rows **strictly older** than ``now - retention_days`` and refuses to
run at all while a legal hold is active, so a hold always wins over age.

Only ``audit_logs`` rows scoped to the workspace are touched; global events
(``workspace_id IS NULL``) are not purged by a workspace policy. Because purge
removes a contiguous oldest prefix, the remaining hash chain is still
verifiable: the earliest surviving row acts as the anchor (see
:mod:`app.services.audit`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import AuditLog, AuditRetentionPolicy, Workspace
from app.services import legal_hold
from app.services.permissions import can

DEFAULT_RETENTION_DAYS = 365
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650

SKIP_LEGAL_HOLD = "legal_hold"


def validate_retention_days(value) -> int:
    """Return ``value`` when it is an integer in range, otherwise raise ValueError.

    ``bool`` is rejected explicitly because it is an ``int`` subclass and a
    truthy flag is never a valid day count.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("retention_days must be an integer.")
    if not MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS:
        raise ValueError(
            f"retention_days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}."
        )
    return value


def policy_for(workspace_id: int) -> AuditRetentionPolicy | None:
    """Return the workspace's policy row, or ``None`` when it uses defaults."""
    return AuditRetentionPolicy.query.filter_by(workspace_id=workspace_id).first()


def policy_snapshot(workspace_id: int) -> dict:
    """Return the effective policy (stored row or defaults) for ``workspace_id``."""
    row = policy_for(workspace_id)
    if row is not None:
        return row.to_dict()
    return {
        "workspace_id": workspace_id,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "legal_hold": False,
        "created_at": None,
        "updated_at": None,
    }


def _workspace(workspace_id: int) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise LookupError("Workspace not found.")
    return workspace


def _require_manager(workspace_id: int, actor) -> None:
    if actor is not None and not can("manage_settings", workspace_id, actor):
        raise PermissionError("You do not have permission to manage audit retention.")


def set_policy(
    workspace_id: int,
    *,
    retention_days: int,
    legal_hold: bool | None = None,
    actor=None,
    commit: bool = True,
) -> dict:
    """Create or update a workspace's retention policy.

    Raises ``ValueError`` for an invalid day count, ``LookupError`` when the
    workspace does not exist, and ``PermissionError`` when ``actor`` lacks the
    owner-level ``manage_settings`` capability.
    """
    _workspace(workspace_id)
    _require_manager(workspace_id, actor)
    days = validate_retention_days(retention_days)

    row = policy_for(workspace_id)
    if row is None:
        row = AuditRetentionPolicy(workspace_id=workspace_id)
        db.session.add(row)
    row.retention_days = days
    if legal_hold is not None:
        row.legal_hold = bool(legal_hold)
    if commit:
        db.session.commit()
    return row.to_dict()


def purge_expired(
    workspace_id: int,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    actor=None,
    commit: bool = True,
) -> dict:
    """Delete workspace audit rows older than the retention window.

    A legal hold makes this a no-op with ``skipped_reason="legal_hold"``; no
    row is ever deleted while the hold is active. Either the legacy policy
    boolean or an active :class:`~app.models.LegalHold` covering the audit scope
    counts as a hold. ``dry_run`` reports the count without deleting. Raises
    ``LookupError`` for an unknown workspace and ``PermissionError`` when
    ``actor`` lacks ``manage_settings``.
    """
    _workspace(workspace_id)
    _require_manager(workspace_id, actor)

    policy = policy_snapshot(workspace_id)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    cutoff = moment - timedelta(days=policy["retention_days"])

    result = {
        "workspace_id": workspace_id,
        "retention_days": policy["retention_days"],
        "legal_hold": policy["legal_hold"],
        "cutoff": cutoff.isoformat(),
        "deleted": 0,
        "dry_run": bool(dry_run),
        "skipped_reason": None,
    }
    if policy["legal_hold"] or legal_hold.has_active_hold(workspace_id, legal_hold.SCOPE_AUDIT):
        result["skipped_reason"] = SKIP_LEGAL_HOLD
        return result

    stale = AuditLog.query.filter(
        AuditLog.workspace_id == workspace_id,
        AuditLog.created_at < cutoff,
    )
    result["deleted"] = int(stale.count() if dry_run else stale.delete(synchronize_session=False))
    if commit and not dry_run:
        db.session.commit()
    return result
