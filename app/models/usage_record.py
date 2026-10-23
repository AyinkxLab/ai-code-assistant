"""Central usage ledger record (Phase 10).

Each row is an immutable accounting fact for one LLM operation: how many
tokens were consumed, which source produced the work, and which user,
workspace, project and conversation it belongs to. Prompts and completions are
deliberately **not** stored here; only metadata and counts are.

Usage is scoped with nullable foreign keys so a record never has to be deleted
when a container is removed:

- deleting a user cascades to their ledger rows;
- deleting a workspace, project or conversation leaves the accounting row in
  place and nulls the corresponding reference (``ON DELETE SET NULL``).
"""

from datetime import UTC, datetime

from app.extensions import db


class UsageRecord(db.Model):
    """One append-only usage entry in the central ledger."""

    __tablename__ = "usage_records"

    #: Composite indexes for the windowed filter + grouping shapes used by the
    #: dashboards, exports and top-entity reports (#436). ``created_at`` is last
    #: in each index so the time-range filter is satisfied by the same scan.
    __table_args__ = (
        db.Index("ix_usage_records_user_created", "user_id", "created_at"),
        db.Index("ix_usage_records_workspace_created", "workspace_id", "created_at"),
        db.Index("ix_usage_records_project_created", "project_id", "created_at"),
        db.Index("ix_usage_records_source_created", "source", "created_at"),
        db.Index(
            "ix_usage_records_provider_model_created",
            "provider",
            "model",
            "created_at",
        ),
        db.Index("ix_usage_records_status_created", "status", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Operation family: chat, analysis, review, tools or project_chat.
    #: Unknown values are preserved so readers can group them safely.
    source = db.Column(db.String(40), nullable=False, default="unknown", index=True)
    #: Optional cost-center / project tag for chargeback rollups.
    cost_center = db.Column(db.String(100), nullable=True, index=True)
    provider = db.Column(db.String(50), nullable=True)
    model = db.Column(db.String(100), nullable=True)
    prompt_tokens = db.Column(db.Integer, nullable=False, default=0)
    completion_tokens = db.Column(db.Integer, nullable=False, default=0)
    total_tokens = db.Column(db.Integer, nullable=False, default=0)
    #: True when any token count was estimated rather than reported by the
    #: provider (streamed replies, the offline mock provider, pre-flight work).
    tokens_estimated = db.Column(db.Boolean, nullable=False, default=True)
    #: Estimated cost in the configured display currency. ``None`` means the
    #: provider/model has no known price; it is never silently reported as zero.
    cost_estimate = db.Column(db.Float, nullable=True)
    #: When the cost estimate was last recomputed after a price change (#440).
    #: ``None`` means the row still carries its original estimate.
    cost_recomputed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    #: Correlation id shared with the request log (``X-Request-ID``). Nullable
    #: because rows can be written by background/CLI work with no request.
    request_id = db.Column(db.String(64), nullable=True, index=True)
    #: End-to-end latency of the instrumented operation, in milliseconds.
    latency_ms = db.Column(db.Integer, nullable=True)
    #: Outcome of the operation: ``success``, ``error`` or ``denied``. Unknown
    #: values are preserved so readers can group historical rows safely.
    status = db.Column(db.String(20), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    def to_dict(self) -> dict:
        """Serialize the record for JSON responses, logs and exports."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "source": self.source,
            "cost_center": self.cost_center,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tokens_estimated": bool(self.tokens_estimated),
            "cost_estimate": self.cost_estimate,
            "cost_recomputed_at": (
                self.cost_recomputed_at.isoformat() if self.cost_recomputed_at else None
            ),
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<UsageRecord id={self.id} source={self.source!r} "
            f"total_tokens={self.total_tokens}>"
        )
