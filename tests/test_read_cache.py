"""Read-cache bounds, TTL, invalidation, and the settings read (#698)."""

from __future__ import annotations

from app.models import Workspace, WorkspaceSettings
from app.services import read_cache, workspace_settings_cache


def test_hit_returns_a_copy_and_miss_returns_none():
    cache = read_cache.ReadCache(ttl=30, max_entries=4)
    assert cache.get("missing") is None
    cache.set("thing", {"items": [1]})
    first = cache.get("thing")
    first["items"].append(2)
    assert cache.get("thing") == {"items": [1]}


def test_entries_expire_after_ttl():
    now = [1000.0]
    cache = read_cache.ReadCache(ttl=10, max_entries=4, clock=lambda: now[0])
    cache.set("key", "value")
    now[0] += 10
    assert cache.get("key") is None
    assert cache.stats()["expirations"] == 1


def test_cache_is_bounded_by_lru_entries():
    cache = read_cache.ReadCache(ttl=60, max_entries=2, clock=lambda: 1000.0)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3
    assert cache.stats()["evictions"] == 1


def test_invalidation_drops_only_the_matching_key_or_prefix():
    cache = read_cache.ReadCache(namespace="test.read", ttl=60, max_entries=10)
    cache.set("workspace:1:settings", "one")
    cache.set("workspace:2:settings", "two")
    cache.invalidate("workspace:1:settings")
    assert cache.get("workspace:1:settings") is None
    assert cache.get("workspace:2:settings") == "two"
    cache.invalidate_prefix("workspace:")
    assert cache.get("workspace:2:settings") is None


def test_workspace_settings_read_is_stale_until_invalidated(app, db, make_user):
    user = make_user()
    workspace = Workspace(user_id=user.id, name="Cache workspace")
    db.session.add(workspace)
    db.session.commit()

    assert workspace_settings_cache.get_workspace_settings_snapshot(workspace.id)[
        "invitations_enabled"
    ]

    row = WorkspaceSettings(workspace_id=workspace.id, invitations_enabled=False)
    db.session.add(row)
    db.session.commit()

    # The first read cached the pre-row default; no write path ran.
    assert workspace_settings_cache.get_workspace_settings_snapshot(workspace.id)[
        "invitations_enabled"
    ]

    workspace_settings_cache.invalidate_workspace_settings(workspace.id)
    assert not workspace_settings_cache.get_workspace_settings_snapshot(workspace.id)[
        "invitations_enabled"
    ]


def test_settings_update_route_invalidates_the_cached_snapshot(client, db, make_user, login):
    user = make_user(username="cacheuser", email="cacheuser@example.com")
    login(email="cacheuser@example.com")
    workspace = Workspace(user_id=user.id, name="Route workspace")
    db.session.add(workspace)
    db.session.commit()

    first = client.get(f"/workspaces/api/workspaces/{workspace.id}/settings")
    assert first.status_code == 200
    assert first.get_json()["invitations_enabled"] is True

    updated = client.put(
        f"/workspaces/api/workspaces/{workspace.id}/settings",
        json={"invitations_enabled": False},
    )
    assert updated.status_code == 200
    assert updated.get_json()["invitations_enabled"] is False

    second = client.get(f"/workspaces/api/workspaces/{workspace.id}/settings")
    assert second.get_json()["invitations_enabled"] is False
