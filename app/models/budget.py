"""Budget and quota model (Phase 10).

A budget applies to exactly one scope - a user or a workspace - for one
monthly period. Both quota types are optional, so a budget may limit tokens,
cost, or both. Enforcement is fail-closed: when a configured limit is reached,
LLM calls are refused before the provider is invoked.
"""

from datetime import UTC, datetime

from app.extensions import db

PERIOD_MONTHLY = "monthly"
PERIODS = (PERIOD_MONTHLY,)

SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"
SCOPES = (SCOPE_USER, SCOPE_WORKSPACE)

DEFAULT_WARN_THRESHOLD = 0.8


class Budget(db.Model):
    """A monthly token and/or cost budget for a user or workspace."""

    __tablename__ = "budgets"

    id = db.Column(db.Integer, primary_key=True)
    #: Exactly one of ``user_id``/``workspace_id`` is set (enforced by a
    #: check constraint). User budgets follow the user; workspace budgets are
    #: owned by the workspace and survive membership changes.
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    period = db.Column(db.String(20), nullable=False, default=PERIOD_MONTHLY)
    #: Maximum total tokens for the period. ``None`` disables the token quota.
    token_limit = db.Column(db.Integer, nullable=True)
    #: Maximum estimated cost for the period. ``None`` disables the cost quota.
    cost_limit = db.Column(db.Float, nullable=True)
    #: Fraction of a limit at which a warning is emitted (0 < threshold <= 1).
    warn_threshold = db.Column(db.Float, nullable=False, default=DEFAULT_WARN_THRESHOLD)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        db.CheckConstraint(
            "(user_id IS NOT NULL AND workspace_id IS NULL) OR "
            "(user_id IS NULL AND workspace_id IS NOT NULL)",
            name="ck_budgets_exactly_one_scope",
        ),
        db.CheckConstraint(
            "token_limit IS NOT NULL OR cost_limit IS NOT NULL",
            name="ck_budgets_has_limit",
        ),
        db.CheckConstraint(
            "warn_threshold > 0 AND warn_threshold <= 1",
            name="ck_budgets_warn_threshold",
        ),
        db.Index("ix_budgets_scope_period", "user_id", "workspace_id", "period"),
    )

    @property
    def warn_threshold_pct(self) -> float:
        """Return the warning threshold as a percentage of the limit.

        The stored value is a fraction (``0.8``); this is the same threshold in
        the percentage form used by the budget UI and API consumers (``80.0``).
        """
        return round(float(self.warn_threshold or DEFAULT_WARN_THRESHOLD) * 100, 2)

    @property
    def scope(self) -> str:
        """Return ``"user"`` or ``"workspace"`` for this budget."""
        return SCOPE_USER if self.user_id is not None else SCOPE_WORKSPACE

    @property
    def scope_id(self) -> int:
        """Return the id of the user or workspace this budget applies to."""
        return self.user_id if self.user_id is not None else self.workspace_id

    def to_dict(self) -> dict:
        """Serialize the budget configuration."""
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "period": self.period,
            "token_limit": self.token_limit,
            "cost_limit": self.cost_limit,
            "warn_threshold": self.warn_threshold,
            "warn_threshold_pct": self.warn_threshold_pct,
            "is_active": bool(self.is_active),
            "created_by_id": self.created_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Budget id={self.id} scope={self.scope} scope_id={self.scope_id} "
            f"tokens={self.token_limit} cost={self.cost_limit}>"
        )
