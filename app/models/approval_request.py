"""Sensitive-action approval requests (#601).

One row records a requested sensitive operation (a role change or a data
deletion), the requester, its lifetime, and the decision made by a *different*
organization administrator. Requests are append-only in effect: a decision is
written in place once, and every transition is mirrored in the audit log.
"""

from datetime import UTC, datetime

from app.extensions import db

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED)

ACTION_ROLE_CHANGE = "role_change"
ACTION_DATA_DELETION = "data_deletion"
SENSITIVE_ACTIONS = (ACTION_ROLE_CHANGE, ACTION_DATA_DELETION)

MAX_ACTION_LENGTH = 64
MAX_TARGET_TYPE_LENGTH = 64
MAX_REASON_LENGTH = 2000


class ApprovalRequest(db.Model):
    """A pending or decided request to perform a sensitive action."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id",
            "action",
            "target_type",
            "target_id",
            "status",
            name="uq_approval_request_scope_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action = db.Column(db.String(MAX_ACTION_LENGTH), nullable=False)
    target_type = db.Column(db.String(MAX_TARGET_TYPE_LENGTH), nullable=False)
    target_id = db.Column(db.Integer, nullable=True)
    #: Bounded JSON object. Secret-like keys are rejected by the service.
    payload = db.Column(db.Text, nullable=False, default="{}")
    status = db.Column(db.String(16), nullable=False, default=STATUS_PENDING, index=True)
    requester_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    decided_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_reason = db.Column(db.Text, nullable=True)

    workspace = db.relationship("Workspace")
    requester = db.relationship("User", foreign_keys=[requester_id])
    decider = db.relationship("User", foreign_keys=[decided_by_id])

    @property
    def is_pending(self) -> bool:
        """Return ``True`` while the request may still be decided."""
        return self.status == STATUS_PENDING

    def to_dict(self) -> dict:
        """Serialize the request for JSON API responses."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "payload": self.payload_json(),
            "status": self.status,
            "requester_id": self.requester_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by_id": self.decided_by_id,
            "decision_reason": self.decision_reason,
        }

    def payload_json(self) -> dict:
        """Return the decoded payload (always a JSON object)."""
        import json

        try:
            value = json.loads(self.payload or "{}")
        except (TypeError, ValueError):  # pragma: no cover - defensive against manual edits
            return {}
        return value if isinstance(value, dict) else {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApprovalRequest id={self.id} action={self.action!r} status={self.status!r}>"
