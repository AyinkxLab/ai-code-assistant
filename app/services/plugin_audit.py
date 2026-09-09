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

from typing import TYPE_CHECKING

from app.models.activity_event import (
    EVENT_PLUGIN_CAPABILITY_GRANTED,
    EVENT_PLUGIN_CAPABILITY_REVOKED,
    EVENT_PLUGIN_DENIED,
    EVENT_PLUGIN_DISABLED,
    EVENT_PLUGIN_ENABLED,
)
from app.services.activity import record_activity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.user import User

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
