"""Tests for plugin lifecycle hooks (#169).

Covers the optional ``on_enable`` / ``on_disable`` / ``on_uninstall`` interface
on ``PluginRegistry``, hook isolation and registry consistency under failure,
the dispatcher lifecycle events (``plugin.enabled`` / ``plugin.disabled`` /
``plugin.uninstalled``), and the API enable/disable/uninstall timing.
"""

import types

import pytest

from app.extensions import db
from app.models import CapabilityGrant, PluginInstallation, Workspace
from app.services.events import get_dispatcher
from app.services.plugins import (
    Plugin,
    PluginManifest,
    PluginRegistrationError,
    PluginRegistry,
)


def _manifest(plugin_id):
    return PluginManifest(
        id=plugin_id,
        name="Lifecycle Plugin",
        version="0.1.0",
        description="test",
        author="tester",
        entry_point="lifecycle_mod:LifecyclePlugin",
        capabilities=["PROJECT_READ"],
    )


def _loaded_plugin(registry, plugin_id, hooks):
    """Register a plugin whose loaded instance records hook calls in ``calls``."""
    manifest = _manifest(plugin_id)
    calls = []

    def make_hook(name):
        def hook(self):
            calls.append(name)

        return hook

    class LifecyclePlugin:
        def __init__(self, app=None, manifest=None):
            pass

    for hook in hooks:
        setattr(LifecyclePlugin, hook, make_hook(hook))
    module = types.ModuleType("lifecycle_mod")
    module.LifecyclePlugin = LifecyclePlugin
    plugin = Plugin(manifest)
    plugin.module = module
    registry.register(plugin)
    return plugin, calls


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Lifecycle workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


class TestRegistryHooks:
    def test_hooks_called_in_order(self):
        registry = PluginRegistry()
        _, calls = _loaded_plugin(registry, "lifecycle", ("on_disable", "on_enable"))
        registry.disable("lifecycle")
        registry.enable("lifecycle")
        assert calls == ["on_disable", "on_enable"]

    def test_absent_hooks_are_noop(self):
        registry = PluginRegistry()
        plugin, _ = _loaded_plugin(registry, "no-hooks", ())
        registry.disable("no-hooks")
        assert plugin.enabled is False
        registry.enable("no-hooks")
        assert plugin.enabled is True

    def test_unloaded_plugin_skips_hooks(self):
        registry = PluginRegistry()
        plugin = Plugin(_manifest("unloaded"))
        registry.register(plugin)  # module is None
        registry.disable("unloaded")
        registry.enable("unloaded")
        assert plugin.enabled is True

    def test_enable_again_does_not_rerun_hook(self):
        registry = PluginRegistry()
        plugin, calls = _loaded_plugin(registry, "idempotent", ("on_enable", "on_disable"))
        registry.disable("idempotent")
        registry.enable("idempotent")
        registry.enable("idempotent")  # already enabled -> no second hook
        assert plugin.enabled is True
        assert calls.count("on_enable") == 1

    def test_failing_enable_hook_keeps_state_consistent(self):
        registry = PluginRegistry()

        class FailingPlugin:
            def __init__(self, app=None, manifest=None):
                pass

            def on_enable(self):
                raise RuntimeError("enable boom")

        module = types.ModuleType("lifecycle_mod")
        module.LifecyclePlugin = FailingPlugin
        plugin = Plugin(_manifest("failing-enable"))
        plugin.module = module
        registry.register(plugin)

        # A raising hook must not prevent the state change or propagate.
        registry.disable("failing-enable")
        registry.enable("failing-enable")
        assert plugin.enabled is True
        assert plugin in registry.list_enabled()

    def test_failing_disable_hook_keeps_state_consistent(self):
        registry = PluginRegistry()

        class FailingPlugin:
            def __init__(self, app=None, manifest=None):
                pass

            def on_disable(self):
                raise RuntimeError("disable boom")

        module = types.ModuleType("lifecycle_mod")
        module.LifecyclePlugin = FailingPlugin
        plugin = Plugin(_manifest("failing-disable"))
        plugin.module = module
        registry.register(plugin)

        registry.disable("failing-disable")
        assert plugin.enabled is False

    def test_uninstall_runs_hook_and_removes_plugin(self):
        registry = PluginRegistry()
        _, calls = _loaded_plugin(
            registry, "uninstall-me", ("on_enable", "on_disable", "on_uninstall")
        )
        registry.uninstall("uninstall-me")
        assert calls == ["on_uninstall"]
        assert registry.get("uninstall-me") is None
        assert len(registry) == 0

    def test_uninstall_still_removes_when_hook_fails(self):
        registry = PluginRegistry()

        class FailingPlugin:
            def __init__(self, app=None, manifest=None):
                pass

            def on_uninstall(self):
                raise RuntimeError("uninstall boom")

        module = types.ModuleType("lifecycle_mod")
        module.LifecyclePlugin = FailingPlugin
        plugin = Plugin(_manifest("failing-uninstall"))
        plugin.module = module
        registry.register(plugin)

        registry.uninstall("failing-uninstall")  # must not raise
        assert registry.get("failing-uninstall") is None

    def test_enable_missing_plugin_still_raises(self):
        registry = PluginRegistry()
        with pytest.raises(PluginRegistrationError):
            registry.enable("ghost")
        with pytest.raises(PluginRegistrationError):
            registry.uninstall("ghost")


class TestApiLifecycle:
    def _install(self, client, workspace_id):
        return client.post(
            f"/plugins/api/workspaces/{workspace_id}/plugins/install",
            json={
                "manifest": {
                    "id": "api-lifecycle",
                    "name": "Lifecycle",
                    "version": "0.1.0",
                    "description": "test",
                    "author": "tester",
                    "entry_point": "plugins.test:TestPlugin",
                    "capabilities": ["PROJECT_READ", "WORKSPACE_READ"],
                }
            },
        )

    def _path(self, workspace_id, action):
        return f"/plugins/api/workspaces/{workspace_id}/plugins/api-lifecycle/{action}"

    def test_enable_disable_uninstall_emit_events(self, app, client, workspace):
        assert self._install(client, workspace.id).status_code == 201

        dispatcher = get_dispatcher()
        seen = []
        handler = lambda event: seen.append(event.event_type)  # noqa: E731
        for event_type in ("plugin.enabled", "plugin.disabled", "plugin.uninstalled"):
            dispatcher.subscribe(event_type, handler)

        try:
            assert client.post(self._path(workspace.id, "disable")).status_code == 200
            assert client.post(self._path(workspace.id, "enable")).status_code == 200
            assert client.post(self._path(workspace.id, "enable")).status_code == 200  # no-op
            assert client.post(self._path(workspace.id, "uninstall")).status_code == 200
            assert client.post(self._path(workspace.id, "uninstall")).status_code == 404
        finally:
            for event_type in ("plugin.enabled", "plugin.disabled", "plugin.uninstalled"):
                dispatcher.unsubscribe(event_type, handler)

        # Each real transition fired exactly once; idempotent re-enable did not.
        assert seen.count("plugin.disabled") == 1
        assert seen.count("plugin.enabled") == 1
        assert seen.count("plugin.uninstalled") == 1

        assert (
            PluginInstallation.query.filter_by(
                plugin_id="api-lifecycle", workspace_id=workspace.id
            ).first()
            is None
        )
        assert (
            CapabilityGrant.query.filter_by(
                plugin_id="api-lifecycle", workspace_id=workspace.id
            ).count()
            == 0
        )

    def test_authorized_plugin_receives_enable_event(self, app, client, workspace):
        assert self._install(client, workspace.id).status_code == 201
        # Grant the capability mapped to plugin.enabled (WORKSPACE_READ).
        grant = client.post(
            f"/plugins/api/workspaces/{workspace.id}/plugins/api-lifecycle/capabilities",
            json={"grant": ["WORKSPACE_READ"], "revoke": []},
        )
        assert grant.status_code == 200

        dispatcher = get_dispatcher()
        seen = []
        handler = lambda event: seen.append(event)  # noqa: E731
        dispatcher.subscribe("plugin.enabled", handler, plugin_id="api-lifecycle")
        try:
            client.post(self._path(workspace.id, "disable"))
            client.post(self._path(workspace.id, "enable"))
        finally:
            dispatcher.unsubscribe("plugin.enabled", handler)

        assert len(seen) == 1
        assert seen[0].data["plugin_id"] == "api-lifecycle"
