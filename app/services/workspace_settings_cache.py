"""Cached snapshot of a workspace's collaboration settings (#698).

The settings row is read on member and notification pages but changes rarely.
Only the serialized, non-secret snapshot is cached; the ORM object stays in the
request session so writes behave normally. Every write path calls
:func:`invalidate_workspace_settings`.
"""

from __future__ import annotations

from app.models import WorkspaceSettings
from app.services import read_cache

DEFAULT_SNAPSHOT = {
    "invitations_enabled": True,
    "default_member_role": "viewer",
}


def _key(workspace_id: int) -> str:
    return f"workspace:{int(workspace_id)}:settings"


def get_workspace_settings_snapshot(workspace_id: int) -> dict:
    """Return the cached settings snapshot, loading it on a miss."""
    key = _key(workspace_id)
    cached = read_cache.get(key)
    if cached is not None:
        return cached

    row = WorkspaceSettings.query.filter_by(workspace_id=workspace_id).first()
    if row is not None:
        snapshot = row.to_dict()
    else:
        snapshot = {
            "workspace_id": workspace_id,
            **DEFAULT_SNAPSHOT,
            "created_at": None,
            "updated_at": None,
        }
    read_cache.set(key, snapshot)
    return snapshot


def invalidate_workspace_settings(workspace_id: int) -> None:
    """Drop the cached snapshot after a settings row is created or changed."""
    read_cache.invalidate(_key(workspace_id))


__all__ = ["get_workspace_settings_snapshot", "invalidate_workspace_settings"]
