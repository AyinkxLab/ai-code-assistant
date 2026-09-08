"""Structured plugin error recording and retrieval.

Records safe, bounded facts about plugin failures and reads them back for the
workspace owner (API) or the operator (CLI). Recording is best-effort: a
failure to record must never surface or break the already-isolated original
error.
"""

from __future__ import annotations

import logging
from typing import Any

from app.extensions import db
from app.models.plugin_error_report import (
    MAX_ERROR_MESSAGE_CHARS,
    PluginErrorReport,
)

logger = logging.getLogger(__name__)

#: Default limits for the read helpers.
WORKSPACE_ERROR_LIMIT = 100
OPERATOR_ERROR_LIMIT = 200


def _safe_message(message: str | None) -> str | None:
    if not message:
        return None
    text = str(message).strip()
    if not text:
        return None
    return text[:MAX_ERROR_MESSAGE_CHARS]


def record_plugin_error(
    plugin_id: str,
    operation: str,
    *,
    exc: BaseException | None = None,
    message: str | None = None,
    workspace_id: int | None = None,
) -> PluginErrorReport | None:
    """Append a bounded plugin error report (never raises on failure).

    ``exc`` contributes the exception type and, when no explicit ``message`` is
    given, a truncated string of the exception. Stack traces and event payloads
    are never stored.
    """
    report = PluginErrorReport(
        plugin_id=str(plugin_id)[:64],
        operation=str(operation)[:160],
        workspace_id=workspace_id,
        exception_type=(
            f"{type(exc).__module__}.{type(exc).__name__}" if exc is not None else None
        ),
        message=_safe_message(message) or _safe_message(str(exc) if exc is not None else None),
    )
    try:
        db.session.add(report)
        db.session.commit()
        return report
    except Exception:  # pragma: no cover - best effort by design
        db.session.rollback()
        logger.warning("Could not record plugin error report", exc_info=True)
        return None


def list_workspace_error_reports(
    workspace_id: int, limit: int = WORKSPACE_ERROR_LIMIT
) -> list[dict[str, Any]]:
    """Return a workspace's plugin error reports, newest first (owner-scoped)."""
    rows = (
        PluginErrorReport.query.filter_by(workspace_id=workspace_id)
        .order_by(PluginErrorReport.id.desc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    return [row.to_dict() for row in rows]


def list_operator_error_reports(limit: int = OPERATOR_ERROR_LIMIT) -> list[dict[str, Any]]:
    """Return the most recent plugin error reports across all scopes (CLI/operator)."""
    rows = (
        PluginErrorReport.query.order_by(PluginErrorReport.id.desc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    return [row.to_dict() for row in rows]
