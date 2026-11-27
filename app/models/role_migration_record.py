"""Undo ledger for the Phase 11 role migration (#605).

Each record stores the exact membership snapshots a migration changed. Holding
the *before* state makes ``scripts/role_migration.py --rollback`` exact instead
of heuristic, and holding the *after* state makes verification auditable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.extensions import db


class RoleMigrationRecord(db.Model):
    """One reversible unit of work from a role migration run."""

    __tablename__ = "role_migration_records"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "user_id", name="uq_role_migration_ws_user"),
        db.Index("ix_role_migration_user_id", "user_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    before_state = db.Column(db.Text, nullable=False, default="[]")
    after_state = db.Column(db.Text, nullable=False, default="[]")
    created_roles = db.Column(db.Text, nullable=False, default="[]")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    @property
    def before(self) -> list[dict]:
        """Return the pre-migration membership snapshots."""
        return _loads(self.before_state)

    @property
    def after(self) -> list[dict]:
        """Return the post-migration membership snapshots."""
        return _loads(self.after_state)

    @property
    def created_role_ids(self) -> list[int]:
        """Return ids of built-in role rows created by the same run."""
        return [item for item in _loads(self.created_roles) if isinstance(item, int)]

    def to_dict(self) -> dict:
        """Serialize the record for structured CLI output."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "before": self.before,
            "after": self.after,
            "created_role_ids": self.created_role_ids,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _loads(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):  # pragma: no cover - defensive on legacy rows
        return []
    return parsed if isinstance(parsed, list) else []


__all__ = ["RoleMigrationRecord"]
