"""Event fan-out to configured Slack, Teams, and email integrations.

Delivery is best-effort: one connector's failure never prevents the others from
running and never bubbles into the request that emitted the event. Only
integrations with a stored credential are contacted, so an unconfigured
workspace performs no network I/O at all.
"""

from __future__ import annotations

import logging

from app.extensions import db
from app.models import Workspace
from app.services.events import get_dispatcher
from app.services.integrations.common import (
    IntegrationError,
    is_configured,
    sanitize_error,
)

logger = logging.getLogger(__name__)

#: Integration name -> module attribute imported lazily inside the loop.
_CONNECTOR_NAMES = ("slack", "teams", "email")


def _connector(name: str):
    from app.services.integrations import email, slack, teams

    return {"slack": slack, "teams": teams, "email": email}[name]


def dispatch_workspace_event(
    workspace_id,
    event_type: str,
    data: dict | None = None,
    *,
    user=None,
) -> list[dict]:
    """Send ``event_type`` to every configured integration for a workspace.

    Returns one bounded result per configured integration. Never raises.
    """
    try:
        workspace_id = int(workspace_id)
    except (TypeError, ValueError):
        return []
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        return []

    results: list[dict] = []
    for name in _CONNECTOR_NAMES:
        try:
            if not is_configured(workspace_id, name):
                continue
            module = _connector(name)
            if name == "email":
                result = module.send_event(workspace, event_type, data, user=user)
            else:
                result = module.send_event(workspace, event_type, data)
            results.append(result)
        except IntegrationError as exc:
            results.append(
                {
                    "status": "error",
                    "integration": name,
                    "code": getattr(exc, "code", "integration_error"),
                    "error": str(exc),
                }
            )
        except Exception as exc:
            logger.warning("Integration %s failed: %s", name, sanitize_error(exc))
            results.append(
                {
                    "status": "error",
                    "integration": name,
                    "code": "integration_error",
                    "error": sanitize_error(exc),
                }
            )
    return results


def _on_review_completed(event) -> None:
    dispatch_workspace_event(
        event.workspace_id,
        "review.completed",
        event.data,
        user=None,
    )


def _on_stellar_completed(event) -> None:
    dispatch_workspace_event(
        event.workspace_id,
        "security.alert",
        {
            "title": "Stellar security analysis completed",
            "summary": "A Stellar/Soroban analysis event was reported.",
            "project_id": (event.data or {}).get("project_id"),
        },
    )


_REGISTERED_HANDLERS: dict[str, object] = {}


def _with_app_context(app, handler):
    """Wrap a handler so it runs with an application context.

    The dispatcher executes handlers in a worker thread, which does not inherit
    the request's Flask application context. Without this wrapper the handler
    could not query integration credentials.
    """

    def wrapped(event):
        if app is None:
            return handler(event)
        with app.app_context():
            return handler(event)

    return wrapped


def register_event_handlers(app=None) -> None:
    """Subscribe the internal integration handlers on the shared dispatcher."""
    dispatcher = get_dispatcher()
    for event_type, handler in (
        ("review.completed", _on_review_completed),
        ("stellar.analysis.completed", _on_stellar_completed),
    ):
        previous = _REGISTERED_HANDLERS.get(event_type)
        if previous is not None:
            dispatcher.unsubscribe(event_type, previous)
        wrapped = _with_app_context(app, handler)
        dispatcher.subscribe(event_type, wrapped)
        _REGISTERED_HANDLERS[event_type] = wrapped


__all__ = ["dispatch_workspace_event", "register_event_handlers"]
