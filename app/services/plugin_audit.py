"""Plugin capability/state security audit trail.

Thin helpers on top of the shared append-only ``ActivityEvent`` log
(``app/services/activity.py``) that record security-relevant plugin actions:
capability granted/revoked, plugin enabled/disabled, and authorization
denials.

Security rules:

* **Never log secrets or payloads.** Only small facts are stored: plugin id,
  capability name, a static reason, action, outcome, and actor.
* **Append-only.** Rows are only ever created next to the action they describe.
* **Fail closed.** Denials are recorded as an *outcome*, not as a leak, and only
  for workspace-scoped events where a plugin was refused.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.extensions import db
from app.models.activity_event import (
    EVENT_PLUGIN_CAPABILITY_GRANTED,
    EVENT_PLUGIN_CAPABILITY_REVOKED,
    EVENT_PLUGIN_DENIED,
    EVENT_PLUGIN_DISABLED,
    EVENT_PLUGIN_ENABLED,
    EVENT_PLUGIN_EXECUTED,
)
from app.services.activity import record_activity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.user import User

logger = logging.getLogger(__name__)

#: Action -> ActivityEvent type for the recorded plugin operations.
_ACTION_EVENT_TYPES = {
    "granted": EVENT_PLUGIN_CAPABILITY_GRANTED,
    "revoked": EVENT_PLUGIN_CAPABILITY_REVOKED,
    "enabled": EVENT_PLUGIN_ENABLED,
    "disabled": EVENT_PLUGIN_DISABLED,
    "denied": EVENT_PLUGIN_DENIED,
}


def record_plugin_audit(
    workspace_id: int,
    action: str,
    *,
    actor: User | None = None,
    plugin_id: str | None = None,
    capability: str | None = None,
    outcome: str = "success",
    reason: str | None = None,
    trigger_event_type: str | None = None,
) -> None:
    """Append an audit event for a plugin security action in this transaction.

    Args:
        workspace_id: Workspace the action affects.
        action: One of ``granted``, ``revoked``, ``enabled``, ``disabled``,
            ``denied``.
        actor: The user who performed the action (may be ``None`` for
            system-originated denials).
        plugin_id: The plugin identifier (may be ``None`` only for actor-side
            denials where the plugin is unknown).
        capability: The capability involved, where applicable.
        outcome: ``success`` or ``denied``.
        reason: A short, static reason (denials only; never payloads).
        trigger_event_type: For dispatch denials, the application event type
            that was refused (never event payloads).
    """
    event_type = _ACTION_EVENT_TYPES.get(action, EVENT_PLUGIN_DENIED)
    metadata: dict[str, str | None] = {"action": action, "outcome": outcome}
    if plugin_id is not None:
        metadata["plugin_id"] = plugin_id
    if capability is not None:
        metadata["capability"] = capability
    if reason is not None:
        metadata["reason"] = reason
    if trigger_event_type is not None:
        metadata["trigger_event_type"] = trigger_event_type
    record_activity(workspace_id, event_type, actor=actor, metadata=metadata)


#: Outcomes accepted for an execution audit row (never free-form text).
EXECUTION_OUTCOMES = frozenset({"success", "error", "timeout", "limit_exceeded"})


def record_plugin_execution(
    workspace_id: int,
    plugin_id: str,
    *,
    version: str | None,
    capability: str | None,
    outcome: str,
    duration_ms: float,
) -> None:
    """Append a bounded execution audit row (never payloads or secrets).

    Only the facts needed to audit a callback are stored: plugin id, version,
    capability, outcome, and duration. Event payloads, configuration, return
    values, and exception messages are deliberately excluded.
    """
    safe_outcome = outcome if outcome in EXECUTION_OUTCOMES else "error"
    metadata: dict[str, str | float] = {
        "action": "executed",
        "plugin_id": str(plugin_id)[:64],
        "outcome": safe_outcome,
        "duration_ms": round(max(0.0, float(duration_ms)), 3),
    }
    if version:
        metadata["version"] = str(version)[:32]
    if capability:
        metadata["capability"] = str(capability)[:64]
    record_activity(workspace_id, EVENT_PLUGIN_EXECUTED, metadata=metadata)
    # Execution rows are written after the action they describe (event handlers
    # are dispatched after the route's own commit), so they must commit
    # themselves. Failure is contained: the callback result is unaffected.
    try:
        db.session.commit()
    except Exception:  # pragma: no cover - best effort by design
        db.session.rollback()
        logger.warning("Could not commit plugin execution audit", exc_info=True)
