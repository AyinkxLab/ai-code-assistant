"""Privacy-safe marketplace install analytics (#695).

Only aggregate counts are stored: a plugin id, an action and a timestamp. There
is no user id, workspace, IP address or user agent, so analytics cannot identify
who installed anything. The dashboard and CSV export read the same aggregates.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from sqlalchemy import func

from app.extensions import db
from app.models.plugin_install_event import ACTIONS, PluginInstallEvent

CSV_HEADER = ("plugin_id", "installs", "updates", "rollbacks", "total")


def record_plugin_event(plugin_id: str, action: str) -> PluginInstallEvent:
    """Record one anonymous event and commit it.

    Raises:
        ValueError: If ``plugin_id`` is empty or ``action`` is not one of
            :data:`~app.models.plugin_install_event.ACTIONS`.
    """
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ValueError("plugin_id is required")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(ACTIONS)}")
    event = PluginInstallEvent(plugin_id=plugin_id.strip(), action=action)
    db.session.add(event)
    db.session.commit()
    return event


def install_counts() -> dict[str, int]:
    """Return cumulative install counts per plugin (used by search ranking)."""
    rows = (
        db.session.query(PluginInstallEvent.plugin_id, func.count(PluginInstallEvent.id))
        .filter(PluginInstallEvent.action == "install")
        .group_by(PluginInstallEvent.plugin_id)
        .all()
    )
    return dict(rows)


def aggregate_events(plugin_ids: Iterable[str] | None = None) -> list[dict]:
    """Return per-plugin install/update/rollback totals, sorted by plugin id.

    Every requested plugin id is present even when it has no events, so the
    dashboard can show a stable zero row instead of hiding the plugin.
    """
    rows = (
        db.session.query(
            PluginInstallEvent.plugin_id,
            PluginInstallEvent.action,
            func.count(PluginInstallEvent.id),
        )
        .group_by(PluginInstallEvent.plugin_id, PluginInstallEvent.action)
        .all()
    )
    counts: dict[str, dict[str, int]] = {}
    for plugin_id, action, count in rows:
        counts.setdefault(plugin_id, {})[action] = count

    if plugin_ids is not None:
        for plugin_id in plugin_ids:
            counts.setdefault(plugin_id, {})
    elif not counts:
        return []

    result = []
    for plugin_id in sorted(counts):
        installs = counts[plugin_id].get("install", 0)
        updates = counts[plugin_id].get("update", 0)
        rollbacks = counts[plugin_id].get("rollback", 0)
        result.append(
            {
                "plugin_id": plugin_id,
                "installs": installs,
                "updates": updates,
                "rollbacks": rollbacks,
                "total": installs + updates + rollbacks,
            }
        )
    return result


def analytics_csv(rows: Iterable[dict]) -> str:
    """Render aggregate rows as CSV with a stable header and ordering."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            (
                row["plugin_id"],
                row["installs"],
                row["updates"],
                row["rollbacks"],
                row["total"],
            )
        )
    return buffer.getvalue()


__all__ = [
    "CSV_HEADER",
    "aggregate_events",
    "analytics_csv",
    "install_counts",
    "record_plugin_event",
]
