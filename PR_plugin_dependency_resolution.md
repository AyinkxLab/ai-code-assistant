# Plug-in dependency resolution

## Summary

Adds an MVP dependency-resolution layer that validates a plugin's declared
dependencies (PEP 508 strings) **before** the plugin is enabled. A plugin whose
required dependencies are missing, disabled, version-incompatible, or cyclic can
no longer be enabled, and is never left partially enabled.

Registration/install behavior is unchanged: dependency validation only guards
**enabling/installing**, so metadata registration does not fail when a
dependency is temporarily unavailable.

## What changed

- **`app/services/plugin_deps.py`** (new) — `DependencyResolver` + `PluginRef`
  and a small exception hierarchy (`PluginDependencyError`,
  `PluginDependencyNotFoundError`, `PluginDependencyDisabledError`,
  `PluginDependencyVersionError`, `PluginDependencyCycleError`,
  `PluginDependencyUnparsableError`). Uses `packaging.requirements.Requirement`
  (already a project dependency) for PEP 508 parsing and
  `packaging.specifiers.SpecifierSet` (via `Requirement.specifier`) for version
  ranges. Python package availability/version is checked with
  `importlib.metadata.version`.
- **`app/services/plugins.py`** — `PluginRegistry.enable()` now resolves
  dependencies before flipping `enabled = True`; raises
  `PluginDependencyResolutionError` with a clear message on failure.
- **`app/services/plugin_ops.py`** — `set_plugin_enabled()` runs the same
  resolution for the persisted plugin store; new `resolve_plugin_dependencies()`
  helper.
- **`app/plugins/routes.py`** — workspace API enable endpoint resolves
  dependencies and returns HTTP 400 with the error text on failure
  (with `db.session.rollback()`).
- **`app/services/plugins_cli.py`** — `enable` command surfaces resolution
  failures with a non-zero exit code.
- **`tests/test_plugin_deps.py`** (new) — 25 tests covering the 12 required
  scenarios: no deps, satisfied plugin/package deps, missing/disabled
  dependencies, version ranges (satisfied and not), direct and indirect cycles,
  recursive satisfied chains, and the registry enable-guard (no partial enable).
- **`tests/test_plugins_cli.py`** — fixture dependency updated so existing CLI
  enable tests pass through the resolver deterministically.

## Approach

- **Plugin vs. Python package** — deterministic rule: if the dependency name
  matches a registered plugin id, it is a plugin dependency; otherwise it is a
  Python package dependency (checked via `importlib.metadata`).
- **Version ranges** — PEP 440 semantics through `packaging`; no custom
  comparison code.
- **Recursive resolution** — DFS over the dependency graph, verifying the full
  transitive chain before enabling.
- **Cycle detection** — a `visiting` path tuple; the reported error includes the
  chain, e.g. `Cannot enable plugin plugin-a: dependency cycle detected:
  plugin-a -> plugin-b -> plugin-a`.
- **No partial enable** — the plugin's `enabled` flag is only set after the
  entire resolution succeeds; no graph library, no package installation, no new
  global state.

## Validation

- `pytest` — 250 plugin-related tests pass, including the 25 new resolver
  tests.
- `ruff check .` — all checks passed.
- `black --check app tests` — 184 files unchanged.

## Test plan

- [x] Plugin with no dependencies enables normally.
- [x] Satisfied plugin/Python dependencies enable normally.
- [x] Missing / disabled / version-incompatible dependencies refuse enable with
      clear errors.
- [x] Direct and indirect cycles detected; enable refused.
- [x] Failed resolution leaves the plugin disabled.