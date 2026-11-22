"""Access review report (#610).

Builds a bounded, read-only snapshot of who can access each workspace, when
they were last active, and whether the access looks stale. The report is
available as JSON or CSV and is intended for periodic compliance reviews.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import User, WorkspaceMember
from app.models.workspace_member import ROLE_OWNER, STATUS_ACTIVE

DEFAULT_STALE_DAYS = 90
MAX_ROWS = 5000

FIELDS = (
    "workspace_id",
    "workspace",
    "user_id",
    "username",
    "email",
    "role",
    "last_active_at",
    "stale",
    "status",
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def build_rows(
    *,
    now: datetime | None = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    workspace_id: int | None = None,
) -> tuple[list[dict], bool]:
    """Return ``(rows, truncated)`` for the access review report.

    Rows are ordered by workspace and user id so repeated reports are stable.
    At most :data:`MAX_ROWS` rows are returned; ``truncated`` says whether more
    access grants existed.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)

    query = WorkspaceMember.query.options(
        joinedload(WorkspaceMember.user), joinedload(WorkspaceMember.workspace)
    ).filter(WorkspaceMember.status == STATUS_ACTIVE)
    if workspace_id is not None:
        query = query.filter(WorkspaceMember.workspace_id == workspace_id)

    members = (
        query.order_by(WorkspaceMember.workspace_id, WorkspaceMember.user_id)
        .limit(MAX_ROWS + 1)
        .all()
    )
    seen: set[tuple[int, int]] = set()
    rows: list[dict] = []

    def add(workspace, user, role: str, last_active: datetime | None) -> None:
        if len(rows) >= MAX_ROWS:
            return
        key = (workspace.id, user.id)
        if key in seen:
            return
        seen.add(key)
        last_active = _as_utc(last_active)
        rows.append(
            {
                "workspace_id": workspace.id,
                "workspace": workspace.name,
                "user_id": user.id,
                "username": user.username,
                "email": user.email,
                "role": role,
                "last_active_at": last_active.isoformat() if last_active else None,
                "stale": last_active is None or last_active < cutoff,
                "status": "active",
            }
        )

    for membership in members:
        if membership.user is None or membership.workspace is None:
            continue
        add(membership.workspace, membership.user, membership.role, membership.last_active_at)

    # Owners are not always materialized as members; include them explicitly.
    for membership in members:
        workspace = membership.workspace
        if workspace is None:
            continue
        owner = db.session.get(User, workspace.user_id)
        if owner is None:
            continue
        add(workspace, owner, ROLE_OWNER, owner.last_login_at)

    truncated = len(members) > MAX_ROWS or len(rows) > MAX_ROWS
    return rows[:MAX_ROWS], truncated


def to_csv(rows: list[dict]) -> str:
    """Render report rows as CSV with a stable column order."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()
