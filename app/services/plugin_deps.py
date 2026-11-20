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
so a cyclic graph fails fast instead of recursing forever. ``resolve_order``
returns the same graph as a deterministic dependency-first install order.

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

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from app.services.plugin_versions import InvalidPluginVersionError, parse_version

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


class PluginDependencyConflictError(PluginDependencyError):
    """Raised when no single version can satisfy the resolved graph's ranges."""


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


def _candidate_versions(specifier_sets) -> set[Version]:
    """Return boundary versions worth probing for a satisfiable intersection.

    PEP 440 does not expose set intersection directly, so conflicts are detected
    by probing the versions named in the constraints (plus the versions either
    side of every boundary). For ordinary ranges this is exact: if none of the
    probes satisfies every specifier, no version can.
    """
    candidates = {Version("0"), Version("1")}
    for specifier_set in specifier_sets:
        for specifier in specifier_set:
            raw = specifier.version
            base = raw[:-2] if raw.endswith(".*") else raw
            try:
                version = Version(base)
            except InvalidVersion:
                continue
            candidates.add(version)
            for suffix in (".post0.dev0", ".post0", ".post1", ".dev0"):
                with contextlib.suppress(InvalidVersion):
                    candidates.add(Version(f"{base}{suffix}"))
            release = version.release
            for index in range(len(release)):
                higher = (
                    *release[:index],
                    release[index] + 1,
                    *(0 for _ in release[index + 1 :]),
                )
                candidates.add(Version(".".join(str(part) for part in higher)))
                if release[index] > 0:
                    lower = (
                        *release[:index],
                        release[index] - 1,
                        *(0 for _ in release[index + 1 :]),
                    )
                    candidates.add(Version(".".join(str(part) for part in lower)))
    return candidates


def _specifiers_conflict(specifier_sets) -> bool:
    """Return ``True`` when the specifier sets share no satisfying version."""
    constrained = [specifier_set for specifier_set in specifier_sets if specifier_set]
    if len(constrained) < 2:
        return False
    return not any(
        all(specifier_set.contains(candidate, prereleases=True) for specifier_set in constrained)
        for candidate in _candidate_versions(constrained)
    )


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
        dependency is missing, disabled, version-incompatible, or cyclic. When
        two paths in the graph constrain the same dependency to disjoint
        ranges, :class:`PluginDependencyConflictError` names both paths so the
        install can be refused before anything changes. The plugin's state is
        never modified here; callers decide how to react.
        """
        constraints: dict[str, list[tuple[Requirement, tuple[str, ...]]]] = {}
        failures: list[tuple[str, Requirement, str]] = []
        self._check_plugin(plugin, (), constraints, failures)
        if failures:
            root_id, requirement, installed = failures[0]
            raise PluginDependencyVersionError(
                root_id,
                f"dependency '{requirement}' is not satisfied by "
                f"installed version {installed}.",
            )
        logger.debug("Plugin dependencies resolved for %s", plugin.id)

    def resolve_order(self, plugin: PluginRef) -> tuple[str, ...]:
        """Return a deterministic dependency-first install order.

        The graph is validated exactly like :meth:`resolve`; a missing,
        disabled, version-incompatible, or cyclic dependency raises the same
        errors before any order is returned. Registered plugin dependencies are
        sorted by canonical id at each level, so the result is stable regardless
        of the order requirements were declared in. Python packages are not
        included: only plugin ids are installable through this ordering. The
        requested plugin is always last.
        """
        self.resolve(plugin)
        order: list[str] = []
        self._collect_install_order(plugin, set(), order)
        return tuple(order)

    def _collect_install_order(
        self,
        plugin: PluginRef,
        visited: set[str],
        order: list[str],
    ) -> None:
        """Post-order walk that emits dependencies before their dependents."""
        if plugin.id in visited:
            return
        visited.add(plugin.id)

        dependencies: list[PluginRef] = []
        for raw in plugin.dependencies:
            try:
                requirement = Requirement(raw.strip())
            except InvalidRequirement:
                # resolve() has already rejected this graph, so this branch is
                # unreachable for callers that came through resolve_order().
                continue
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            dependency = self._plugin_provider(requirement.name)
            if dependency is not None:
                dependencies.append(dependency)

        for dependency in sorted(dependencies, key=lambda item: canonicalize_name(item.id)):
            self._collect_install_order(dependency, visited, order)
        order.append(plugin.id)

    # ---------------- internals ----------------

    def _register_constraint(
        self,
        constraints: dict[str, list[tuple[Requirement, tuple[str, ...]]]],
        requirement: Requirement,
        path: tuple[str, ...],
    ) -> None:
        """Record a version constraint and reject a disjoint intersection.

        ``path`` is the chain of plugin ids from the requested root to the
        plugin that declared this requirement, so a conflict can report exactly
        which paths disagree.
        """
        if not requirement.specifier:
            return
        key = canonicalize_name(requirement.name)
        entries = constraints.setdefault(key, [])
        entries.append((requirement, path))
        if len(entries) < 2:
            return
        if not _specifiers_conflict([entry[0].specifier for entry in entries]):
            return
        details = "; ".join(
            f"{entry_path[-1]} requires '{entry_requirement}' " f"(path: {' -> '.join(entry_path)})"
            for entry_requirement, entry_path in entries
        )
        raise PluginDependencyConflictError(
            path[0],
            f"version conflict for '{requirement.name}': {details}",
        )

    def _check_plugin(
        self,
        plugin: PluginRef,
        visiting: tuple[str, ...],
        constraints: dict[str, list[tuple[Requirement, tuple[str, ...]]]],
        failures: list[tuple[str, Requirement, str]],
    ) -> None:
        """Recursively validate ``plugin``'s dependencies.

        ``visiting`` holds the plugin ids on the current path from the root
        (used for cycle detection); a tuple keeps it immutable and cheap.
        Version mismatches are collected and reported after the whole graph has
        been walked, so a conflict between two branches is reported as a
        conflict rather than as whichever branch happened to fail first.
        """
        if plugin.id in visiting:
            cycle = (*visiting, plugin.id)
            raise PluginDependencyCycleError(
                plugin.id,
                "dependency cycle detected: " + " -> ".join(cycle),
            )

        path = (*visiting, plugin.id)
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
            self._register_constraint(constraints, requirement, path)
            dependency = self._plugin_provider(requirement.name)
            if dependency is not None:
                self._check_plugin_dependency(
                    path[0], requirement, dependency, path, constraints, failures
                )
            else:
                self._check_package_dependency(path[0], requirement, failures)

    def _check_plugin_dependency(
        self,
        root_id: str,
        requirement: Requirement,
        dependency: PluginRef,
        visiting: tuple[str, ...],
        constraints: dict[str, list[tuple[Requirement, tuple[str, ...]]]],
        failures: list[tuple[str, Requirement, str]],
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
            failures.append((root_id, requirement, dependency.version))
        # Keep walking even on a version mismatch so constraints declared below
        # this dependency can still participate in conflict detection.
        self._check_plugin(dependency, visiting, constraints, failures)

    def _check_package_dependency(
        self,
        root_id: str,
        requirement: Requirement,
        failures: list[tuple[str, Requirement, str]],
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
            version = parse_version(installed)
        except InvalidPluginVersionError:
            raise PluginDependencyVersionError(
                root_id,
                f"dependency '{requirement}' cannot be checked against "
                f"installed version {installed!r}.",
            ) from None

        specifier = requirement.specifier
        if specifier and not specifier.contains(version, prereleases=True):
            failures.append((root_id, requirement, installed))


class CatalogDependencyGraph:
    """Build and validate a catalog entry's transitive dependency graph.

    Unlike :class:`DependencyResolver`, which checks the *installed* environment
    at enable time, this builder walks the operator-controlled catalog before an
    install writes anything. Only dependencies whose names match a catalog
    entry are traversed; Python package requirements and unknown plugin ids are
    left for enable-time resolution, so a catalog install never requires a
    dependency to already be installed.

    The graph is deterministic: duplicate edges/nodes are collapsed and
    independent branches are visited in canonical-id order, producing a
    dependency-first topological order. Cycles are detected with a visiting
    path and reported with the full chain (for example ``a -> b -> a``).
    """

    def __init__(self, catalog) -> None:
        self._catalog = catalog
        self._entries_by_name: dict[str, list] = {}
        for entry in catalog.list_entries():
            self._entries_by_name.setdefault(canonicalize_name(entry.plugin_id), []).append(entry)

    def _entry_for_requirement(self, requirement: Requirement, path: tuple[str, ...]):
        """Return the newest catalog entry satisfying ``requirement``, if any."""
        matches = self._entries_by_name.get(canonicalize_name(requirement.name), [])
        if not matches:
            # Not a catalogued plugin: could be a Python package or a plugin
            # installed from another source. Enable-time validation handles it.
            return None
        for entry in reversed(matches):
            version = Version(entry.version)
            if not requirement.specifier or requirement.specifier.contains(
                version, prereleases=True
            ):
                return entry
        raise PluginDependencyVersionError(
            path[0],
            f"dependency '{requirement}' has no catalogued version satisfying it "
            f"(path: {' -> '.join(path)}).",
        )

    def _dependencies(self, entry, path: tuple[str, ...]):
        """Return deduplicated, sorted ``(canonical_id, entry)`` dependencies."""
        resolved: dict[tuple[str, Version], object] = {}
        raw_dependencies = entry.manifest.get("dependencies") or ()
        if not isinstance(raw_dependencies, (list, tuple)):
            raise PluginDependencyUnparsableError(
                entry.plugin_id,
                f"Invalid dependency list on catalog entry {entry.plugin_id!r}.",
            )
        for raw in raw_dependencies:
            try:
                requirement = Requirement(str(raw).strip())
            except InvalidRequirement as exc:
                raise PluginDependencyUnparsableError(
                    entry.plugin_id,
                    f"Invalid dependency requirement: {raw!r} ({exc})",
                ) from exc
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            dependency = self._entry_for_requirement(requirement, path)
            if dependency is None:
                continue
            key = (canonicalize_name(dependency.plugin_id), Version(dependency.version))
            resolved.setdefault(key, dependency)
        return [resolved[key] for key in sorted(resolved, key=lambda item: (item[0], item[1]))]

    def validate(self, entry) -> tuple[str, ...]:
        """Return the dependency-first topological order for ``entry``.

        Raises:
            PluginDependencyCycleError: With the full chain when a cycle exists.
            PluginDependencyUnparsableError: For an invalid requirement string.
            PluginDependencyVersionError: When a catalogued dependency exists but
                no catalogued version satisfies the declared range.
        """
        return tuple(node.plugin_id for node in self.ordered_entries(entry))

    def ordered_entries(self, entry) -> tuple[object, ...]:
        """Return catalog entries in dependency-first install order.

        The same validation as :meth:`validate` runs first, so a caller gets no
        ordering (and therefore writes nothing) for a cyclic, unparsable, or
        version-unsatisfiable graph.
        """
        order: list[object] = []
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(node) -> None:
            if node.plugin_id in visiting:
                index = visiting.index(node.plugin_id)
                cycle = [*visiting[index:], node.plugin_id]
                raise PluginDependencyCycleError(
                    node.plugin_id,
                    "dependency cycle detected: " + " -> ".join(cycle),
                )
            canonical_id = canonicalize_name(node.plugin_id)
            if canonical_id in visited:
                return

            visiting.append(node.plugin_id)
            for dependency in self._dependencies(node, tuple(visiting)):
                visit(dependency)
            visiting.pop()

            visited.add(canonical_id)
            order.append(node)

        visit(entry)
        return tuple(order)


def validate_catalog_dependencies(catalog, entry) -> tuple[str, ...]:
    """Validate a catalog entry's transitive dependencies before install.

    Returns the deterministic dependency-first order. Non-catalog dependencies
    are intentionally not checked here; the existing enable-time resolver stays
    authoritative for their installed state.
    """
    return CatalogDependencyGraph(catalog).validate(entry)


def resolve_install_order(catalog, entry) -> tuple[object, ...]:
    """Return the full dependency-first catalog entries to install (#362).

    Every node is resolved from the catalog and validated (cycles, unparsable
    requirements, and unsatisfiable version ranges) before the caller writes
    anything, so an install is all-or-nothing.
    """
    return CatalogDependencyGraph(catalog).ordered_entries(entry)
