"""Append-only audit log for sensitive actions (issue #34).

Every entry records who did what to which target and when. Rows are written by
:mod:`app.services.audit` and are never updated or deleted through the
application — there is no edit/delete route for them, and the admin endpoint is
read-only. A user deletion keeps their history by nulling ``user_id``.
"""

import json
from datetime import UTC, datetime

from app.extensions import db


class AuditLog(db.Model):
    """A single append-only audit entry."""

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = db.Column(db.String(100), nullable=False, index=True)
    target_type = db.Column(db.String(50), nullable=True)
    target_id = db.Column(db.Integer, nullable=True)
    #: JSON blob of contextual details. The attribute is named ``meta`` because
    #: ``metadata`` is reserved by SQLAlchemy's declarative base; the database
    #: column is still called ``metadata``.
    meta = db.Column("metadata", db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    user = db.relationship("User")

    @property
    def metadata_dict(self) -> dict | None:
        """The parsed metadata mapping, or ``None`` when absent/invalid."""
        if not self.meta:
            return None
        try:
            data = json.loads(self.meta)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def to_dict(self) -> dict:
        """Serialize the entry for the admin JSON API."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else None,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "metadata": self.metadata_dict,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditLog id={self.id} action={self.action!r} user_id={self.user_id}>"
