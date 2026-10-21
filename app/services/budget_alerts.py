"""Budget threshold alert evaluation (Phase 10, issue #471).

The ledger records usage; this module decides when a workspace's spend crosses
its budget limit. Alerts are deduplicated per workspace and period so a burst of
usage produces one notification instead of one per request. The in-process
deduplicator is intentionally small and resettable (tests and multi-process
workers each keep their own view); a persistent dedupe store can replace it
without changing the public :func:`check_budget_alerts` contract.

When SMTP is configured, over-budget alerts are also delivered by email through
the existing email service. Email delivery is best-effort and never raises; the
in-app notification remains the fallback and source of truth.
"""

from __future__ import annotations

import logging

from app.services.token_usage import BudgetAlertDeduplicator

logger = logging.getLogger(__name__)

#: Alert kind emitted when spend reaches the warning threshold (default 80%).
ALERT_WARNING = "warning"

#: Alert kind emitted when spend is above the configured limit.
ALERT_OVER_BUDGET = "over_budget"

#: Default warning threshold as a fraction of the limit.
DEFAULT_WARN_RATIO = 0.8

_deduplicator = BudgetAlertDeduplicator()


def _send_alert_email(alert: dict) -> bool:
    """Best-effort email delivery of a budget alert.

    Returns `True` when the email was accepted by the transport and `False`
    otherwise (SMTP unconfigured, no recipient, delivery failure). Never
    raises: any failure is logged and swallowed so the in-app notification
    remains the fallback.
    """
    try:
        from flask import current_app

        recipient = current_app.config.get("BILLING_ALERT_EMAIL")
        if not recipient:
            logger.debug(
                "Budget alert has no recipient; skipping email for workspace %s",
                alert.get("workspace_id"),
            )
            return False

        from app.services.email import send_alert_email

        workspace_id = alert.get("workspace_id")
        period = alert.get("period") or ""
        spend = alert.get("spend")
        limit = alert.get("limit")
        over_by = alert.get("over_by")

        subject = f"Budget alert for workspace {workspace_id} ({period})"
        text_body = (
            f"Workspace {workspace_id} has exceeded its budget for period {period}.\n"
            f"Spend: {spend}\n"
            f"Limit: {limit}\n"
            f"Over budget by: {over_by}\n"
        )
        html_body = (
            "<p>Workspace <strong>"
            f"{workspace_id}</strong> has exceeded its budget for period "
            f"<strong>{period}</strong>.</p>"
            f"<p>Spend: {spend}<br />Limit: {limit}<br />"
            f"Over budget by: {over_by}</p>"
        )
        return send_alert_email(recipient, subject, text_body, html_body)
    except Exception as exc:  # pragma: no cover - email is best-effort
        logger.warning("Failed to deliver budget alert email: %s", exc)
        return False


def check_budget_alerts(
    workspace_id,
    *,
    spend,
    limit,
    period,
    warn_ratio: float = DEFAULT_WARN_RATIO,
) -> list[dict]:
    """Return the budget threshold alerts crossed for `workspace_id` (#371).

    `spend` and `limit` are compared as numbers. A missing, zero or negative
    limit means no budget is configured, so no alert is emitted.

    Alerts fire at the **warning** threshold (default 80% of the limit) and the
    **over-budget** threshold (100%). Each threshold fires at most once per
    workspace/period: a burst of usage yields one alert per crossed threshold,
    and starting a new period resets deduplication. Jumping straight past the
    warning to over-budget still emits both crossed thresholds once.

    When SMTP is configured and a billing recipient is set, the over-budget
    alert is also emailed. Email failures never affect the returned alerts.
    """
    try:
        spend_value = float(spend or 0)
        limit_value = float(limit or 0)
        ratio = float(warn_ratio)
    except (TypeError, ValueError):
        return []
    if limit_value <= 0:
        return []

    period_key = (workspace_id, str(period or ""))
    reached = spend_value / limit_value
    alerts: list[dict] = []

    def build(kind: str) -> dict:
        return {
            "workspace_id": workspace_id,
            "kind": kind,
            "period": str(period or ""),
            "spend": spend_value,
            "limit": limit_value,
            "ratio": round(reached, 6),
            "over_by": round(max(0.0, spend_value - limit_value), 6),
        }

    if reached >= 1.0:
        # Crossing the limit implies the warning was also crossed.
        if _deduplicator.check_and_mark(period_key, ALERT_WARNING):
            alerts.append(build(ALERT_WARNING))
        if _deduplicator.check_and_mark(period_key, ALERT_OVER_BUDGET):
            over = build(ALERT_OVER_BUDGET)
            alerts.append(over)
            _send_alert_email(over)
    elif reached >= ratio and _deduplicator.check_and_mark(period_key, ALERT_WARNING):
        alerts.append(build(ALERT_WARNING))

    return alerts


def notify_budget_alerts(alerts, users, *, workspace=None, link=None) -> list:
    """Create in-app notifications for `users`, honouring their preferences.

    A user who has opted out of ``budget_alerts`` receives nothing; this
    delegates the preference check to
    :func:`app.services.notifications.notify`. Returns the created
    notifications.
    """
    if not alerts:
        return []
    from app.services.notifications import notify

    created = []
    for user in users:
        if user is None:
            continue
        for alert in alerts:
            notification = notify(
                user,
                "budget_alert",
                workspace=workspace if workspace is not None else alert.get("workspace_id"),
                payload=alert,
                link=link,
                email=alert.get("kind") == ALERT_OVER_BUDGET,
            )
            if notification is not None:
                created.append(notification)
    return created


def reset() -> None:
    """Forget deduplication state (used between tests/periods)."""
    global _deduplicator
    _deduplicator = BudgetAlertDeduplicator()


__all__ = [
    "ALERT_OVER_BUDGET",
    "ALERT_WARNING",
    "DEFAULT_WARN_RATIO",
    "check_budget_alerts",
    "notify_budget_alerts",
    "reset",
]
