"""Plugin dependency resolution and validation.

Declared plugin dependencies are PEP 508 requirement strings (e.g.
``plugin-a>=1.0``, ``requests>=2.31,<3``). This module verifies, at the moment a
plugin is enabled, that every declared dependency is currently satisfiable:

* a dependency whose name matches a **registered plugin id** is a plugin
  dependency — it must be registered, enabled, and satisfy any declared version
  range;
* anything else is treated as a **Python package** dependency — it must be
  installed (checked via ``importlib.metadata``) at a version satisfying the
  declared range.

Resolution is recursive (a plugin's dependency graph is walked before the plugin
is considered safe to enable) and detects cycles with an on-path visiting set,
so a cyclic graph fails fast instead of recursing forever.

It only *verifies* availability; it never installs packages or modifies ``pip``
environments. The distinction between plugin and package dependencies is
deliberately simple: a name that matches a registered plugin id is a plugin
dependency, otherwise a Python package dependency. No heuristics beyond that.

The resolver operates on :class:`PluginRef` records rather than on any
particular plugin store, so callers (the in-memory ``PluginRegistry`` and the
persisted ``Plugin`` rows) both reuse it without the resolver knowing which
store a plugin came from.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)


class PluginDependencyError(Exception):
    """Base exception for plugin dependency resolution failures."""

    def __init__(self, plugin_id: str, message: str) -> None:
        super().__init__(message)
        self.plugin_id = plugin_id
        self.message = message


class PluginDependencyCycleError(PluginDependencyError):
    """Raised when the plugin dependency graph contains a cycle."""


class PluginDependencyNotFoundError(PluginDependencyError):
    """Raised when a required dependency is not registered/installed."""


class PluginDependencyDisabledError(PluginDependencyError):
    """Raised when a required plugin dependency is disabled."""


class PluginDependencyVersionError(PluginDependencyError):
    """Raised when a dependency's version does not satisfy a declared range."""


class PluginDependencyUnparsableError(PluginDependencyError):
    """Raised when a declared dependency is not a valid PEP 508 requirement."""


@dataclass(frozen=True)
class PluginRef:
    """Normalized plugin record consumed by the resolver.

    ``id``, ``version``, and ``enabled`` mirror the fields every plugin store
    exposes; ``dependencies`` are the plugin's declared PEP 508 requirement
    strings (default ``()``).
    """

    id: str
    version: str
    enabled: bool
    dependencies: tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        return f"<PluginRef {self.id} v{self.version}>"


#: Look up a plugin dependency by id; returns ``None`` when not registered.
PluginProvider = Callable[[str], PluginRef | None]

#: Read an installed distribution's version; raises PackageNotFoundError.
PackageVersion = Callable[[str], str]


class DependencyResolver:
    """Resolve and validate a plugin's declared dependencies.

    The resolver is intentionally small and stateless-per-call. Each
    ``resolve`` call walks the dependency graph with its own visiting set, so a
    single instance can safely serve many plugins (no cached global state).
    """

    def __init__(
        self,
        plugin_provider: PluginProvider,
        *,
        package_version: PackageVersion | None = None,
    ) -> None:
        self._plugin_provider = plugin_provider
        #: Overridable for tests; defaults to installed distribution metadata.
        self._package_version = package_version or metadata.version

    def resolve(self, plugin: PluginRef) -> None:
        """Verify ``plugin`` and all of its transitive dependencies.

        Raises a :class:`PluginDependencyError` subclass when any required
        dependency is missing, disabled, version-incompatible, or cyclic. The
        plugin's state is never modified here; callers decide how to react.
        """
        self._check_plugin(plugin, ())
        logger.debug("Plugin dependencies resolved for %s", plugin.id)

    # ---------------- internals ----------------

    def _check_plugin(self, plugin: PluginRef, visiting: tuple[str, ...]) -> None:
        """Recursively validate ``plugin``'s dependencies.

        ``visiting`` holds the plugin ids on the current path from the root
        (used for cycle detection); a tuple keeps it immutable and cheap.
        """
        if plugin.id in visiting:
            cycle = (*visiting, plugin.id)
            raise PluginDependencyCycleError(
                plugin.id,
                "dependency cycle detected: " + " -> ".join(cycle),
            )

        visiting = (*visiting, plugin.id)
        for raw in plugin.dependencies:
            try:
                requirement = Requirement(raw.strip())
            except InvalidRequirement as exc:
                raise PluginDependencyUnparsableError(
                    plugin.id,
                    f"Invalid dependency requirement: {raw!r} ({exc})",
                ) from exc
            if requirement.marker is not None and not requirement.marker.evaluate():
                # Environment markers (e.g. ``sys_platform == "win32"``) that do
                # not apply to this environment are ignored.
                continue
            dependency = self._plugin_provider(requirement.name)
            if dependency is not None:
                self._check_plugin_dependency(plugin.id, requirement, dependency, visiting)
            else:
                self._check_package_dependency(plugin.id, requirement)

    def _check_plugin_dependency(
        self,
        root_id: str,
        requirement: Requirement,
        dependency: PluginRef,
        visiting: tuple[str, ...],
    ) -> None:
        """Validate a dependency that refers to a registered plugin."""
        if dependency.id in visiting:
            # A cycle is reported before enabled/version checks: the graph is
            # invalid no matter the states involved, and the caller needs the
            # chain (``a -> b -> a``), not a state error on the loop-closing
            # plugin.
            cycle = (*visiting, dependency.id)
            raise PluginDependencyCycleError(
                root_id,
                "dependency cycle detected: " + " -> ".join(cycle),
            )
        if not dependency.enabled:
            raise PluginDependencyDisabledError(
                root_id,
                f"required plugin dependency '{requirement.name}' is disabled.",
            )
        if requirement.specifier and not requirement.specifier.contains(
            dependency.version, prereleases=True
        ):
            raise PluginDependencyVersionError(
                root_id,
                f"dependency '{requirement}' is not satisfied by "
                f"installed version {dependency.version}.",
            )
        self._check_plugin(dependency, visiting)

    def _check_package_dependency(
        self,
        root_id: str,
        requirement: Requirement,
    ) -> None:
        """Validate a dependency that refers to an installed Python package."""
        try:
            installed = self._package_version(requirement.name)
        except metadata.PackageNotFoundError:
            raise PluginDependencyNotFoundError(
                root_id,
                f"required Python package '{requirement}' is not installed.",
            ) from None

        try:
            version = Version(installed)
        except InvalidVersion:
            raise PluginDependencyVersionError(
                root_id,
                f"dependency '{requirement}' cannot be checked against "
                f"installed version {installed!r}.",
            ) from None

        specifier = requirement.specifier
        if specifier and not specifier.contains(version, prereleases=True):
            raise PluginDependencyVersionError(
                root_id,
                f"dependency '{requirement}' is not satisfied by "
                f"installed version {installed}.",
            )
