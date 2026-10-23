"""Monthly token and cost budgets (Phase 10).

Budgets are configured per user or per workspace and enforced *before* an LLM
call. Both quota types are optional and independent:

* ``token_limit`` caps total tokens recorded in the monthly usage ledger.
* ``cost_limit`` caps the estimated cost of those tokens.

A call is refused when either configured limit is reached. When both are
reached the refusal reports every limit that was hit, so a client can tell
whether the token quota, the cost quota, or both caused it.

Enforcement is **fail closed**: if a budget cannot be evaluated, the call is
refused rather than allowed to spend unbounded tokens. Budgets are only a
gate; the append-only ledger remains the source of truth for usage.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps

from flask import current_app, g, jsonify, request
from flask_login import current_user
from sqlalchemy import func, or_

from app.extensions import db
from app.models.budget import (
    DEFAULT_WARN_THRESHOLD,
    PERIOD_MONTHLY,
    PERIODS,
    SCOPE_USER,
    SCOPE_WORKSPACE,
    Budget,
)
from app.models.project import Project
from app.models.usage_record import UsageRecord
from app.services import audit, notifications

logger = logging.getLogger(__name__)

#: Ledger statuses that represent work that did *not* happen. Denied calls must
#: not count toward a budget, otherwise a blocked caller would keep increasing
#: their own spend. Unknown/legacy statuses count conservatively so a budget
#: cannot be bypassed by omitting the field.
EXCLUDED_STATUSES = ("denied",)

#: ``(budget_id, period_start, level)`` keys already notified in this process.
#: Notifications are advisory; the limiter prevents duplicate alerts from the
#: same worker. Multi-worker deployments may emit at most one per worker.
_ALERTS: set[tuple[int, str, str]] = set()
_ALERTS_LOCK = threading.Lock()


class BudgetCheckError(RuntimeError):
    """Raised when budgets cannot be evaluated (fail-closed condition)."""


class BudgetValidationError(ValueError):
    """Raised when budget configuration is invalid."""


@dataclass(frozen=True)
class BudgetStatus:
    """Usage against one budget."""

    budget: Budget
    period_start: datetime
    tokens_used: int
    cost_used: float
    token_ratio: float | None
    cost_ratio: float | None
    limits_hit: tuple[str, ...]
    warned: bool
    #: Costs in the ledger are estimates, never provider invoices.
    cost_is_estimate: bool = True

    @property
    def scope(self) -> str:
        return self.budget.scope

    @property
    def scope_id(self) -> int:
        return self.budget.scope_id

    @property
    def allowed(self) -> bool:
        return not self.limits_hit

    def to_dict(self) -> dict:
        data = self.budget.to_dict()
        data.update(
            {
                "period_start": self.period_start.isoformat(),
                "tokens_used": self.tokens_used,
                "cost_used": round(self.cost_used, 6),
                "token_ratio": (
                    round(self.token_ratio, 4) if self.token_ratio is not None else None
                ),
                "cost_ratio": (round(self.cost_ratio, 4) if self.cost_ratio is not None else None),
                "limits_hit": list(self.limits_hit),
                "warned": self.warned,
                "allowed": self.allowed,
                "cost_is_estimate": self.cost_is_estimate,
                "usage_is_estimated": True,
            }
        )
        return data


@dataclass(frozen=True)
class BudgetDecision:
    """Aggregate decision across every applicable budget."""

    allowed: bool
    warned: bool
    limits_hit: tuple[str, ...] = ()
    statuses: tuple[BudgetStatus, ...] = ()
    retry_after: int = 0
    message: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "warned": self.warned,
            "limits_hit": list(self.limits_hit),
            "retry_after": self.retry_after,
            "message": self.message,
            "budgets": [status.to_dict() for status in self.statuses],
        }


def period_start(now: datetime | None = None) -> datetime:
    """Return the first instant of the current UTC month."""
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_period_start(now: datetime | None = None) -> datetime:
    """Return the first instant of the following UTC month."""
    start = period_start(now)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def seconds_until_next_period(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    return max(1, int((next_period_start(now) - now).total_seconds()))


def enforcement_enabled() -> bool:
    """Whether budget enforcement is enabled for this application."""
    return bool(current_app.config.get("BUDGET_ENFORCEMENT_ENABLED", True))


def warn_threshold_default() -> float:
    try:
        value = float(current_app.config.get("BUDGET_DEFAULT_WARN_THRESHOLD", 0.8))
    except (TypeError, ValueError):
        return DEFAULT_WARN_THRESHOLD
    return min(max(value, 0.01), 1.0)


def threshold_ratio(budget: Budget) -> float:
    """Return ``budget``'s warning threshold as a fraction of its limit.

    The column is constrained to ``0 < warn_threshold <= 1``, but rows can be
    built in memory (tests, fixtures, imports) before validation runs, so the
    value is clamped and falls back to the documented default when unusable.
    """
    try:
        value = float(budget.warn_threshold)
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_WARN_THRESHOLD
    return min(max(value, 0.01), 1.0)


def budgets_for(user, workspace_id: int | None = None) -> list[Budget]:
    """Return active monthly budgets applicable to ``user`` and ``workspace``."""
    budgets: list[Budget] = []
    authenticated = user is not None and getattr(user, "is_authenticated", False)
    if authenticated:
        budget = (
            Budget.query.filter_by(
                user_id=user.id, workspace_id=None, period=PERIOD_MONTHLY, is_active=True
            )
            .order_by(Budget.updated_at.desc())
            .first()
        )
        if budget is not None:
            budgets.append(budget)
    if workspace_id is not None:
        budget = (
            Budget.query.filter_by(
                workspace_id=workspace_id,
                user_id=None,
                period=PERIOD_MONTHLY,
                is_active=True,
            )
            .order_by(Budget.updated_at.desc())
            .first()
        )
        if budget is not None:
            budgets.append(budget)
    return budgets


def usage_totals(budget: Budget, now: datetime | None = None) -> tuple[int, float]:
    """Return ``(tokens, cost)`` recorded in the budget's current period."""
    start = period_start(now)
    end = next_period_start(now)
    query = db.session.query(
        func.coalesce(func.sum(UsageRecord.total_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost_estimate), 0.0),
    ).filter(
        UsageRecord.created_at >= start,
        UsageRecord.created_at < end,
        or_(
            UsageRecord.status.is_(None),
            ~UsageRecord.status.in_(EXCLUDED_STATUSES),
        ),
    )
    if budget.user_id is not None:
        query = query.filter(UsageRecord.user_id == budget.user_id)
    else:
        query = query.filter(UsageRecord.workspace_id == budget.workspace_id)
    tokens, cost = query.one()
    return int(tokens or 0), float(cost or 0.0)


def evaluate_budget(budget: Budget, now: datetime | None = None) -> BudgetStatus:
    """Evaluate one budget against current-period usage."""
    tokens, cost = usage_totals(budget, now)
    limits_hit: list[str] = []
    warned = False
    threshold = threshold_ratio(budget)

    token_ratio = None
    if budget.token_limit is not None:
        limit = max(0, int(budget.token_limit))
        token_ratio = float("inf") if limit == 0 else tokens / limit
        if tokens >= limit:
            limits_hit.append("tokens")
        elif token_ratio >= threshold:
            warned = True

    cost_ratio = None
    if budget.cost_limit is not None:
        limit = max(0.0, float(budget.cost_limit))
        cost_ratio = float("inf") if limit == 0 else (cost / limit if limit else 0.0)
        if cost >= limit:
            limits_hit.append("cost")
        elif cost_ratio >= threshold:
            warned = True

    return BudgetStatus(
        budget=budget,
        period_start=period_start(now),
        tokens_used=tokens,
        cost_used=cost,
        token_ratio=token_ratio,
        cost_ratio=cost_ratio,
        limits_hit=tuple(limits_hit),
        warned=warned,
    )


def check_budgets(user, workspace_id: int | None = None) -> BudgetDecision:
    """Evaluate every applicable budget and return the aggregate decision.

    Raises :class:`BudgetCheckError` when evaluation fails so callers can fail
    closed instead of silently allowing the call.
    """
    if not enforcement_enabled():
        return BudgetDecision(allowed=True, warned=False)

    try:
        budgets = budgets_for(user, workspace_id)
        if not budgets:
            return BudgetDecision(allowed=True, warned=False)
        statuses = tuple(evaluate_budget(budget) for budget in budgets)
    except Exception as exc:  # pragma: no cover - defensive fail-closed path
        logger.exception("budget evaluation failed")
        raise BudgetCheckError("budget evaluation failed") from exc

    limits: list[str] = []
    blocked: list[str] = []
    for status in statuses:
        for limit in status.limits_hit:
            if limit not in limits:
                limits.append(limit)
        if not status.allowed:
            blocked.append(f"{status.scope} {status.budget.period}")
    warned = any(status.warned for status in statuses)
    allowed = not blocked

    message = ""
    if not allowed:
        parts = []
        for status in statuses:
            if not status.allowed:
                parts.append(_status_message(status))
        message = "Budget exceeded: " + " ".join(parts)
    elif warned:
        message = "Approaching a configured budget: " + " ".join(
            _status_message(status) for status in statuses if status.warned
        )

    return BudgetDecision(
        allowed=allowed,
        warned=warned,
        limits_hit=tuple(limits),
        statuses=statuses,
        retry_after=0 if allowed else seconds_until_next_period(),
        message=message,
    )


def _status_message(status: BudgetStatus) -> str:
    scope = f"{status.scope} budget"
    if status.limits_hit == ("tokens", "cost"):
        return (
            f"{scope} hit both its token limit "
            f"({status.tokens_used}/{status.budget.token_limit}) and cost limit "
            f"({status.cost_used:.4f}/{status.budget.cost_limit:.4f})."
        )
    if status.limits_hit == ("tokens",):
        return (
            f"{scope} hit its token limit "
            f"({status.tokens_used}/{status.budget.token_limit} tokens)."
        )
    if status.limits_hit == ("cost",):
        return (
            f"{scope} hit its cost limit "
            f"({status.cost_used:.4f}/{status.budget.cost_limit:.4f})."
        )
    return f"{scope} is near its configured limit."


def budget_error_response(decision: BudgetDecision):
    """Build the JSON 429 response used when a budget blocks an LLM call."""
    response = jsonify(
        {
            "error": decision.message or "Budget exceeded.",
            "kind": "budget_exceeded",
            "limits_hit": list(decision.limits_hit),
            "retry_after": decision.retry_after,
            "budgets": [status.to_dict() for status in decision.statuses],
        }
    )
    response.status_code = 429
    response.headers["Retry-After"] = str(decision.retry_after)
    return response


def _workspace_id_for_request(kwargs: dict) -> int | None:
    workspace_id = kwargs.get("workspace_id")
    if workspace_id is not None:
        return int(workspace_id)
    project_id = kwargs.get("project_id")
    if project_id is not None:
        project = db.session.get(Project, int(project_id))
        return project.workspace_id if project is not None else None
    conversation_id = kwargs.get("conversation_id")
    if conversation_id is not None:
        from app.models.conversation import Conversation

        conversation = db.session.get(Conversation, int(conversation_id))
        return conversation.workspace_id if conversation is not None else None
    review_id = kwargs.get("review_id")
    if review_id is not None:
        from app.models.review import Review

        review = db.session.get(Review, int(review_id))
        if review is not None and review.project_id is not None:
            project = db.session.get(Project, review.project_id)
            return project.workspace_id if project is not None else None
    data = request.get_json(silent=True) if request.is_json else None
    if isinstance(data, dict):
        if data.get("workspace_id") is not None:
            try:
                return int(data["workspace_id"])
            except (TypeError, ValueError):
                return None
        if data.get("project_id") is not None:
            try:
                project = db.session.get(Project, int(data["project_id"]))
            except (TypeError, ValueError):
                return None
            return project.workspace_id if project is not None else None
    return None


def _record_refusal(user, workspace_id: int | None) -> None:
    """Record why a billed call was refused, without ever failing the request."""
    from flask import request

    from app.services import usage_ledger

    endpoint = (getattr(request, "endpoint", "") or "").lower()
    if "review" in endpoint:
        source = usage_ledger.SOURCE_REVIEW
    elif "tool" in endpoint:
        source = usage_ledger.SOURCE_TOOLS
    elif "chat" in endpoint:
        source = usage_ledger.SOURCE_CHAT
    elif "workspace" in endpoint or "project" in endpoint:
        source = usage_ledger.SOURCE_ANALYSIS
    else:
        source = usage_ledger.UNKNOWN_SOURCE

    try:
        usage_ledger.record_usage(
            source=source,
            user=user,
            workspace_id=workspace_id,
            status=usage_ledger.STATUS_DENIED,
            tokens_estimated=True,
        )
    except Exception:
        # Audit/accounting must never turn a refusal into a crash.
        return


def budget_refusal(user, workspace_id: int | None = None):
    """Return a refusal response when a billable call cannot proceed.

    This is the single decision point shared by the :func:`budget_guard`
    decorator and code paths that request AI work without being a dedicated
    billable route (for example an issue fetch with ``?analyze=1``). Returns
    ``None`` when the call is allowed, otherwise a ``429`` response for an
    exceeded limit or a ``503`` response when evaluation fails (fail closed).
    """
    try:
        decision = check_budgets(user, workspace_id)
    except BudgetCheckError:
        _record_refusal(user, workspace_id)
        response = jsonify(
            {
                "error": "Budget check unavailable; refusing the request.",
                "kind": "budget_check_failed",
            }
        )
        response.status_code = 503
        return response

    g.budget_decision = decision
    if not decision.allowed:
        _record_refusal(user, workspace_id)
        notify_decision(user, decision, level="blocked")
        return budget_error_response(decision)
    if decision.warned:
        notify_decision(user, decision, level="warning")
    return None


#: Attribute stamped on view functions protected by :func:`budget_guard`.
#: The enforcement-coverage test uses it to prove that every billable endpoint
#: still enforces the hard limit when new routes are added.
BUDGET_ENFORCED_ATTR = "budget_enforced"


def budget_guard(view):
    """Decorator enforcing budgets before an LLM-calling view runs.

    The resolved decision is exposed as ``g.budget_decision`` so a view can
    include the warning in its response. On refusal the request never reaches
    the provider.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not enforcement_enabled():
            return view(*args, **kwargs)
        refusal = budget_refusal(current_user, _workspace_id_for_request(kwargs))
        if refusal is not None:
            return refusal
        return view(*args, **kwargs)

    setattr(wrapped, BUDGET_ENFORCED_ATTR, True)
    return wrapped


def notify_decision(user, decision: BudgetDecision, *, level: str) -> list:
    """Notify affected users once per budget/period/level.

    Runs in the caller's transaction. Duplicate suppression is process-local,
    matching the existing budget-alert deduplicator.
    """
    created = []
    link = "/billing/"
    for status in decision.statuses:
        if level == "blocked" and status.allowed:
            continue
        if level == "warning" and not status.warned:
            continue
        budget = status.budget
        key = (budget.id, status.period_start.isoformat(), level)
        with _ALERTS_LOCK:
            if key in _ALERTS:
                continue
            _ALERTS.add(key)

        recipients = []
        if budget.user_id is not None:
            recipients.append(user)
        else:
            from app.models.user import User
            from app.models.workspace import Workspace

            workspace = db.session.get(Workspace, budget.workspace_id)
            if workspace is not None:
                recipients.append(db.session.get(User, workspace.user_id))
            if user is not None:
                recipients.append(user)

        seen_ids = set()
        for recipient in recipients:
            if recipient is None or recipient.id in seen_ids:
                continue
            seen_ids.add(recipient.id)
            notification = notifications.notify(
                recipient,
                "budget_alert",
                workspace=budget.workspace_id,
                payload={
                    "level": level,
                    "scope": status.scope,
                    "period": budget.period,
                    "period_start": status.period_start.isoformat(),
                    "limits_hit": list(status.limits_hit),
                    "tokens_used": status.tokens_used,
                    "token_limit": budget.token_limit,
                    "cost_used": round(status.cost_used, 6),
                    "cost_limit": budget.cost_limit,
                    "warn_threshold": budget.warn_threshold,
                    "message": decision.message,
                },
                link=link,
            )
            if notification is not None:
                created.append(notification)
    return created


def after_usage_recorded(record: UsageRecord) -> None:
    """Evaluate budgets after a ledger row is written and notify on thresholds.

    Called from :func:`app.services.usage_ledger.record_usage` inside the
    caller's transaction. Failures are logged and never break accounting.
    """
    try:
        from app.models.user import User

        user = db.session.get(User, record.user_id) if record.user_id is not None else None
        if user is None and record.workspace_id is None:
            return
        decision = check_budgets(user, record.workspace_id)
        if decision.warned or not decision.allowed:
            notify_decision(
                user,
                decision,
                level="blocked" if not decision.allowed else "warning",
            )
    except Exception:  # pragma: no cover - accounting must not fail
        logger.exception("post-usage budget evaluation failed")


# ---------------------------------------------------------------------------
# Configuration helpers used by the billing API/UI
# ---------------------------------------------------------------------------


def validate_limits(token_limit, cost_limit) -> tuple[int | None, float | None]:
    """Validate and normalize quota limits. At least one must be set."""
    normalized_tokens = None
    normalized_cost = None
    if token_limit is not None and token_limit != "":
        try:
            normalized_tokens = int(token_limit)
        except (TypeError, ValueError) as exc:
            raise BudgetValidationError("token_limit must be an integer.") from exc
        if normalized_tokens < 0:
            raise BudgetValidationError("token_limit must be zero or greater.")
    if cost_limit is not None and cost_limit != "":
        try:
            normalized_cost = float(cost_limit)
        except (TypeError, ValueError) as exc:
            raise BudgetValidationError("cost_limit must be a number.") from exc
        if normalized_cost < 0:
            raise BudgetValidationError("cost_limit must be zero or greater.")
    if normalized_tokens is None and normalized_cost is None:
        raise BudgetValidationError("Set a token limit, a cost limit, or both.")
    return normalized_tokens, normalized_cost


def validate_threshold(value) -> float:
    if value is None or value == "":
        return warn_threshold_default()
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise BudgetValidationError("warn_threshold must be a number.") from exc
    if not 0 < threshold <= 1:
        raise BudgetValidationError("warn_threshold must be greater than 0 and at most 1.")
    return threshold


def validate_period(period: str | None) -> str:
    period = (period or PERIOD_MONTHLY).strip().lower()
    if period not in PERIODS:
        raise BudgetValidationError("Only monthly budgets are supported.")
    return period


#: Allowlisted budget fields captured in the audit diff. Never widen this to a
#: raw request payload: budget metadata must not be able to carry secrets.
BUDGET_DIFF_FIELDS = ("period", "token_limit", "cost_limit", "warn_threshold", "is_active")
MAX_BUDGET_DIFF_VALUE_CHARS = 120


def _bounded_diff_value(value):
    """Return a JSON-safe, size-bounded value for the audit diff."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_BUDGET_DIFF_VALUE_CHARS]


def _budget_snapshot(budget: Budget | None) -> dict:
    if budget is None:
        return dict.fromkeys(BUDGET_DIFF_FIELDS)
    return {
        field: _bounded_diff_value(getattr(budget, field, None)) for field in BUDGET_DIFF_FIELDS
    }


def _budget_diff(before: dict, after: dict) -> dict:
    return {
        field: {"before": before[field], "after": after[field]}
        for field in BUDGET_DIFF_FIELDS
        if before[field] != after[field]
    }


def _actor_audit_kwargs(actor) -> dict:
    if actor is None:
        return {}
    if hasattr(actor, "id"):
        return {"user": actor}
    if isinstance(actor, int):
        return {"user_id": actor}
    return {}


def upsert_budget(
    *,
    scope: str,
    scope_id: int,
    token_limit=None,
    cost_limit=None,
    warn_threshold=None,
    period=None,
    actor=None,
    commit: bool = True,
) -> Budget:
    """Create or replace the active budget for a scope and period."""
    if scope not in (SCOPE_USER, SCOPE_WORKSPACE):
        raise BudgetValidationError("scope must be 'user' or 'workspace'.")
    tokens, cost = validate_limits(token_limit, cost_limit)
    threshold = validate_threshold(warn_threshold)
    period = validate_period(period)

    query = Budget.query.filter_by(period=period)
    if scope == SCOPE_USER:
        query = query.filter_by(user_id=scope_id, workspace_id=None)
    else:
        query = query.filter_by(workspace_id=scope_id, user_id=None)
    budget = query.order_by(Budget.updated_at.desc()).first()
    created = budget is None
    before = _budget_snapshot(budget)

    if budget is None:
        budget = Budget(
            user_id=scope_id if scope == SCOPE_USER else None,
            workspace_id=scope_id if scope == SCOPE_WORKSPACE else None,
            period=period,
            created_by_id=getattr(actor, "id", None),
        )
        db.session.add(budget)
    budget.token_limit = tokens
    budget.cost_limit = cost
    budget.warn_threshold = threshold
    budget.is_active = True
    budget.updated_at = datetime.now(UTC)

    # Flush first so a newly created row has an id for the audit target.
    db.session.flush()
    after = _budget_snapshot(budget)
    audit.record(
        audit.BUDGET_CREATED if created else audit.BUDGET_UPDATED,
        target_type="budget",
        target_id=budget.id,
        metadata={
            "scope": budget.scope,
            "scope_id": budget.scope_id,
            "diff": _budget_diff(before, after),
        },
        **_actor_audit_kwargs(actor),
    )

    if commit:
        db.session.commit()
    return budget


def delete_budget(budget: Budget, *, commit: bool = True) -> None:
    db.session.delete(budget)
    if commit:
        db.session.commit()


def statuses_for_user(user, workspace_ids: list[int] | None = None) -> list[dict]:
    """Return current status for a user's budget and owned workspace budgets."""
    rows: list[dict] = []
    for budget in budgets_for(user, None):
        rows.append(evaluate_budget(budget).to_dict())
    for workspace_id in workspace_ids or []:
        for budget in budgets_for(user, workspace_id):
            if budget.workspace_id == workspace_id:
                rows.append(evaluate_budget(budget).to_dict())
    return rows


def reset_alerts() -> None:
    """Clear process-local alert deduplication (tests and worker restarts)."""
    with _ALERTS_LOCK:
        _ALERTS.clear()


#: Alias used by the shared test reset fixture.
reset = reset_alerts


__all__ = [
    "BUDGET_ENFORCED_ATTR",
    "BudgetCheckError",
    "BudgetDecision",
    "BudgetStatus",
    "BudgetValidationError",
    "after_usage_recorded",
    "budget_error_response",
    "budget_guard",
    "budget_refusal",
    "budgets_for",
    "check_budgets",
    "delete_budget",
    "enforcement_enabled",
    "evaluate_budget",
    "next_period_start",
    "notify_decision",
    "period_start",
    "reset_alerts",
    "seconds_until_next_period",
    "statuses_for_user",
    "threshold_ratio",
    "upsert_budget",
    "usage_totals",
    "validate_limits",
    "validate_period",
    "validate_threshold",
]
