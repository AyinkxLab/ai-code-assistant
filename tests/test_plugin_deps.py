"""Tests for plugin dependency resolution (#168).

Covers the resolver's core behaviors: no-dependency plugins enable normally,
plugin and Python package dependencies are validated (presence, enabled state,
version ranges), dependency graphs resolve recursively, and cycles are rejected
with a clear chain. All package checks go through an injected ``package_version``
callable so the suite never depends on packages installed on the developer's
machine.
"""

from importlib import metadata

import pytest

from app.services.plugin_deps import (
    DependencyResolver,
    PluginDependencyCycleError,
    PluginDependencyDisabledError,
    PluginDependencyNotFoundError,
    PluginDependencyUnparsableError,
    PluginDependencyVersionError,
    PluginRef,
)
from app.services.plugins import (
    Plugin,
    PluginDependencyResolutionError,
    PluginManifest,
    PluginRegistry,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _ref(plugin_id, version="1.0.0", enabled=True, dependencies=()):
    return PluginRef(
        id=plugin_id,
        version=version,
        enabled=enabled,
        dependencies=tuple(dependencies),
    )


def _resolver(plugins, installed=None):
    """Build a resolver over a ``{id: PluginRef}`` mapping.

    ``installed`` maps a package name to its installed version; a name absent
    from ``installed`` is treated as not installed (any package not listed).
    """
    installed = installed if installed is not None else {}
    provider = lambda name: plugins.get(name)  # noqa: E731

    def package_version(name):
        if name not in installed:
            raise metadata.PackageNotFoundError(name)
        return installed[name]

    return DependencyResolver(provider, package_version=package_version)


def _registry_plugin(plugin_id, version="0.1.0", dependencies=(), enabled=True):
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id.replace("-", " ").title(),
        version=version,
        description="test",
        author="tester",
        entry_point="plugins.test:TestPlugin",
        capabilities=["PROJECT_READ"],
        dependencies=list(dependencies),
    )
    plugin = Plugin(manifest)
    plugin.enabled = enabled
    return plugin


# ---------------------------------------------------------------------------
# Resolver-level: no dependencies / satisfied Python packages
# ---------------------------------------------------------------------------


class TestNoDependencies:
    def test_no_dependencies_resolves(self):
        _resolver({}).resolve(_ref("plugin-a"))

    def test_registry_enables_plugin_without_dependencies(self):
        registry = PluginRegistry()
        plugin = _registry_plugin("plugin-a")
        registry.register(plugin)
        registry.enable("plugin-a")
        assert plugin.enabled is True


class TestPythonPackageDependencies:
    def test_satisfied_package(self):
        resolver = _resolver({}, installed={"requests": "2.31.0"})
        resolver.resolve(_ref("plugin-a", dependencies=["requests>=2.31"]))

    def test_satisfied_package_version_range(self):
        resolver = _resolver({}, installed={"requests": "2.31.0"})
        resolver.resolve(_ref("plugin-a", dependencies=["requests>=2.31,<3"]))

    def test_missing_package(self):
        resolver = _resolver({}, installed={})
        with pytest.raises(PluginDependencyNotFoundError) as excinfo:
            resolver.resolve(_ref("plugin-a", dependencies=["some-missing-pkg>=1.0"]))

        assert excinfo.value.plugin_id == "plugin-a"
        assert "some-missing-pkg" in excinfo.value.message
        assert "not installed" in excinfo.value.message

    def test_package_version_mismatch(self):
        resolver = _resolver({}, installed={"some-package": "1.5.0"})
        with pytest.raises(PluginDependencyVersionError) as excinfo:
            resolver.resolve(_ref("plugin-a", dependencies=["some-package>=2.0"]))

        assert excinfo.value.plugin_id == "plugin-a"
        assert "some-package>=2.0" in excinfo.value.message
        assert "1.5.0" in excinfo.value.message

    def test_invalid_installed_version_reports_mismatch(self):
        resolver = _resolver({}, installed={"broken-pkg": "not-a-version"})
        with pytest.raises(PluginDependencyVersionError):
            resolver.resolve(_ref("plugin-a", dependencies=["broken-pkg>=1.0"]))

    def test_environment_marker_not_matching_is_ignored(self):
        # A marker that cannot apply on this environment is skipped.
        resolver = _resolver({}, installed={})
        resolver.resolve(
            _ref("plugin-a", dependencies=["some-missing-pkg>=1.0; python_version < '1.0'"])
        )


# ---------------------------------------------------------------------------
# Resolver-level: plugin dependencies
# ---------------------------------------------------------------------------


class TestPluginDependencies:
    def test_satisfied_plugin_dependency(self):
        plugins = {"plugin-b": _ref("plugin-b", version="1.0.0", enabled=True)}
        resolver = _resolver(plugins)
        resolver.resolve(_ref("plugin-a", dependencies=["plugin-b>=1.0"]))

    def test_missing_plugin_dependency(self):
        resolver = _resolver({})
        with pytest.raises(PluginDependencyNotFoundError) as excinfo:
            resolver.resolve(_ref("plugin-a", dependencies=["plugin-missing>=1.0"]))

        assert excinfo.value.plugin_id == "plugin-a"
        assert "plugin-missing" in excinfo.value.message

    def test_disabled_plugin_dependency(self):
        plugins = {"plugin-b": _ref("plugin-b", version="1.0.0", enabled=False)}
        resolver = _resolver(plugins)
        with pytest.raises(PluginDependencyDisabledError) as excinfo:
            resolver.resolve(_ref("plugin-a", dependencies=["plugin-b"]))

        assert excinfo.value.plugin_id == "plugin-a"
        assert "plugin-b" in excinfo.value.message
        assert "disabled" in excinfo.value.message

    def test_plugin_version_range_satisfied(self):
        plugins = {"plugin-b": _ref("plugin-b", version="1.5.0", enabled=True)}
        resolver = _resolver(plugins)
        resolver.resolve(_ref("plugin-a", dependencies=["plugin-b>=1.0,<2.0"]))

    def test_plugin_version_range_not_satisfied(self):
        plugins = {"plugin-b": _ref("plugin-b", version="1.5.0", enabled=True)}
        resolver = _resolver(plugins)
        with pytest.raises(PluginDependencyVersionError) as excinfo:
            resolver.resolve(_ref("plugin-a", dependencies=["plugin-b>=2.0"]))

        assert excinfo.value.plugin_id == "plugin-a"
        assert "plugin-b>=2.0" in excinfo.value.message
        assert "1.5.0" in excinfo.value.message

    def test_invalid_dependency_string(self):
        with pytest.raises(PluginDependencyUnparsableError) as excinfo:
            _resolver({}).resolve(_ref("plugin-a", dependencies=["not a valid requirement !!"]))

        assert excinfo.value.plugin_id == "plugin-a"

    def test_recursive_satisfied_dependencies(self):
        plugins = {
            "plugin-b": _ref("plugin-b", enabled=True, dependencies=["plugin-c"]),
            "plugin-c": _ref("plugin-c", enabled=True),
        }
        _resolver(plugins).resolve(_ref("plugin-a", dependencies=["plugin-b"]))


# ---------------------------------------------------------------------------
# Resolver-level: cycles
# ---------------------------------------------------------------------------


class TestCycles:
    def test_direct_cycle(self):
        plugins = {
            "plugin-a": _ref("plugin-a", enabled=True, dependencies=["plugin-b"]),
            "plugin-b": _ref("plugin-b", enabled=True, dependencies=["plugin-a"]),
        }
        with pytest.raises(PluginDependencyCycleError) as excinfo:
            _resolver(plugins).resolve(_ref("plugin-a", dependencies=["plugin-b"]))

        assert "plugin-a -> plugin-b -> plugin-a" in excinfo.value.message

    def test_indirect_cycle(self):
        plugins = {
            "plugin-a": _ref("plugin-a", enabled=True, dependencies=["plugin-b"]),
            "plugin-b": _ref("plugin-b", enabled=True, dependencies=["plugin-c"]),
            "plugin-c": _ref("plugin-c", enabled=True, dependencies=["plugin-a"]),
        }
        with pytest.raises(PluginDependencyCycleError) as excinfo:
            _resolver(plugins).resolve(_ref("plugin-a", dependencies=["plugin-b"]))

        assert "plugin-a -> plugin-b -> plugin-c" in excinfo.value.message
        assert "plugin-a" in excinfo.value.message


# ---------------------------------------------------------------------------
# Registry-level: enable() guard and no-partial-enable behavior
# ---------------------------------------------------------------------------


class TestRegistryEnableGuard:
    def test_enable_satisfied_dependencies(self):
        registry = PluginRegistry()
        registry.register(_registry_plugin("plugin-b"))
        plugin_a = _registry_plugin("plugin-a", dependencies=["plugin-b"], enabled=False)
        registry.register(plugin_a)
        registry.enable("plugin-a")
        assert plugin_a.enabled is True

    def test_enable_refused_for_missing_plugin_dependency(self):
        registry = PluginRegistry()
        plugin_a = _registry_plugin("plugin-a", dependencies=["plugin-missing"], enabled=False)
        registry.register(plugin_a)
        with pytest.raises(PluginDependencyResolutionError) as excinfo:
            registry.enable("plugin-a")

        assert plugin_a.enabled is False
        assert "plugin-missing" in str(excinfo.value)

    def test_enable_refused_for_disabled_plugin_dependency(self):
        registry = PluginRegistry()
        registry.register(_registry_plugin("plugin-b", enabled=False))
        plugin_a = _registry_plugin("plugin-a", dependencies=["plugin-b"], enabled=False)
        registry.register(plugin_a)
        with pytest.raises(PluginDependencyResolutionError) as excinfo:
            registry.enable("plugin-a")

        assert plugin_a.enabled is False
        assert "disabled" in str(excinfo.value)

    def test_enable_refused_for_missing_package(self, monkeypatch):
        def no_packages(name):
            raise metadata.PackageNotFoundError(name)

        registry = PluginRegistry()
        monkeypatch.setattr(metadata, "version", no_packages)
        plugin_a = _registry_plugin("plugin-a", dependencies=["some-missing-pkg"], enabled=False)
        registry.register(plugin_a)
        with pytest.raises(PluginDependencyResolutionError) as excinfo:
            registry.enable("plugin-a")

        assert plugin_a.enabled is False
        assert "some-missing-pkg" in str(excinfo.value)

    def test_enable_refused_for_cycle(self):
        registry = PluginRegistry()
        registry.register(_registry_plugin("plugin-a", dependencies=["plugin-b"], enabled=False))
        registry.register(_registry_plugin("plugin-b", dependencies=["plugin-a"]))
        with pytest.raises(PluginDependencyResolutionError) as excinfo:
            registry.enable("plugin-a")

        assert "dependency cycle detected" in str(excinfo.value)
        assert "plugin-a -> plugin-b -> plugin-a" in str(excinfo.value)
        assert registry.get("plugin-a").enabled is False

    def test_enable_refused_for_version_mismatch(self):
        registry = PluginRegistry()
        registry.register(_registry_plugin("plugin-b", version="1.5.0"))
        plugin_a = _registry_plugin("plugin-a", dependencies=["plugin-b>=2.0"], enabled=False)
        registry.register(plugin_a)
        with pytest.raises(PluginDependencyResolutionError) as excinfo:
            registry.enable("plugin-a")

        assert plugin_a.enabled is False
        assert "plugin-b>=2.0" in str(excinfo.value)

    def test_registration_does_not_require_satisfied_dependencies(self):
        # Registration must not fail merely because a dependency is unavailable;
        # only enabling is guarded.
        registry = PluginRegistry()
        plugin_a = _registry_plugin("plugin-a", dependencies=["plugin-missing"])
        registry.register(plugin_a)
        assert registry.get("plugin-a") is not None

    def test_already_enabled_plugin_is_idempotent(self):
        registry = PluginRegistry()
        plugin = _registry_plugin("plugin-a", enabled=True)
        registry.register(plugin)
        registry.enable("plugin-a")  # must not re-resolve or raise
        assert plugin.enabled is True
