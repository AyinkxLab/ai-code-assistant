# Plugin System

The AI Code Assistant ships an extensible plugin architecture so that Stellar,
Soroban, GitHub, AI-provider, and developer-tooling features can be added
without modifying the core application.

> **Status:** Phase 8. Manifest validation, the registry, the capability model,
> the event system with fail-closed dispatch-time capability enforcement, and a
> workspace-scoped plugin management API + UI are implemented and tested.
> Dependency resolution, compatibility/versioning, signing/trust, and a
> marketplace are **not** implemented yet — see [Contributing](#contributing).

## Concepts

| Concept             | Description                                                        |
| ------------------- | ------------------------------------------------------------------ |
| Manifest            | `manifest.json` describing a plugin (id, version, capabilities…).  |
| Plugin              | A runtime plugin instance built from a manifest.                   |
| Registry            | Central registration, discovery, lookup, enable/disable.           |
| Capability          | A named permission a plugin may request (never granted by default).|
| Capability grant    | An explicit per-workspace grant row; nothing is granted implicitly.|
| Event / hook        | A plugin can subscribe to supported events and react.              |
| Event dispatcher    | Dispatches events to subscribers; handler failures are isolated.   |

## Plugin manifest

A plugin lives in its own directory and is described by `manifest.json`. The
JSON Schema is at [`plugins/plugin.schema.json`](../plugins/plugin.schema.json).

```json
{
  "id": "stellar-tools",
  "name": "Stellar Developer Tools",
  "version": "0.1.0",
  "description": "Soroban / Stellar analysis helpers.",
  "author": "AI Code Assistant Team",
  "entry_point": "plugins.stellar_tools.plugin:StellarPlugin",
  "compatibility": ">=0.8.0",
  "capabilities": ["PROJECT_READ", "STELLAR_READ", "AI_ACCESS"],
  "permissions": ["read:project:files"],
  "dependencies": [],
  "configuration": {
    "networks": ["testnet", "mainnet"]
  }
}
```

### Manifest validation

`app/services/plugins.py::PluginManifest.from_dict` rejects manifests that:

- Miss a required field (`id`, `name`, `version`, `description`, `author`,
  `entry_point`, `capabilities`).
- Use an invalid `id` (must match `^[a-z][a-z0-9_-]*$`).
- Use an invalid semantic version.
- Use an invalid `entry_point` (must be `module.path:ClassName`).
- Declare an unknown capability.
- Declare an empty capability list.
- Declare an invalid `compatibility` specifier (when present, it must be valid
  PEP 440 syntax such as `>=0.8.0` or `>=0.1.0,<0.5.0`).

Validation errors raise `ManifestValidationError`; unreadable or malformed
manifest files raise `PluginError`.

### Compatibility (PEP 440)

The optional `compatibility` field declares the application versions a plugin
supports (`==`, `!=`, `<=`, `>=`, `<`, `>`, `~=`, wildcards, and comma/space
separated ranges). Enforcement lives in
`app/services/plugin_compat.py` and is compared against the running app version
(installed package metadata, falling back to `pyproject.toml`):

- **Omitted / empty** → the plugin supports any application version.
- **Invalid** specifier → the manifest is rejected during validation.
- **Incompatible** range → installation is refused with a clear message naming
  the required range and the current app version.
- Compatible plugins register normally, and the management API exposes a
  compatibility badge in plugin metadata (`compatibility`,
  `compatible_with_app`, and `app_version`).

## Registry

`PluginRegistry` (in `app/services/plugins.py`) provides:

- `register(plugin)` — duplicate plugin ids raise `PluginRegistrationError`.
- `discover(dir)` — scan a directory for `manifest.json` files; invalid
  manifests are skipped (with a warning), never fatal.
- `get(plugin_id)` / `list_all()` / `list_enabled()`.
- `enable(plugin_id)` / `disable(plugin_id)`.
- `validate_capability(plugin_id, capability)`.

## Capabilities

Capabilities are defined in `app/services/capabilities.py` and include
`PROJECT_READ`, `PROJECT_WRITE`, `GITHUB_READ`, `GITHUB_WRITE`, `AI_ACCESS`,
`AI_ANALYSIS`, `WORKSPACE_READ`, `WORKSPACE_WRITE`, `NOTIFICATION_CREATE`,
`STELLAR_READ`, `STELLAR_WRITE`, `STELLAR_ANALYSIS`, `REVIEW_READ`,
`REVIEW_CREATE`, `PROJECT_DELETE`.

Capabilities are **never granted automatically**. A plugin receives a
capability only when an explicit `CapabilityGrant` row exists for that plugin
in a workspace (`CapabilityStore.grant`). The workspace role model maps each
role to the capabilities it may exercise (`ROLE_CAPABILITY_MAPPING`), and
`validate_plugin_capability` fails closed.

The `CapabilityGrant` model lives at `app/models/plugin.py` (table
`plugin_capability_grants`) and is created by the Phase 8 migration.

## Events / hooks

`app/services/events.py` provides an `EventDispatcher` and a global instance
via `get_dispatcher()`. Supported event types include:

- `project.created`, `project.updated`, `project.deleted`
- `review.created`, `review.completed`, `review.finding.added`
- `workspace.created`, `workspace.member_added`, `workspace.member_removed`
- `github.connected`, `github.disconnected`
- `ai.analysis.completed`
- `stellar.analysis.completed`, `stellar.network.detected`

Plugins subscribe with `dispatcher.subscribe(event_type, handler, plugin_id)`.
Subscribing to an unsupported event type raises `EventError`. When an event is
dispatched, one failing handler never prevents other handlers from running, and
the request itself is never crashed (routes use `emit_event`, which swallows
and logs failures).

### Event capability enforcement (fail closed)

Before a **plugin** handler is invoked, the dispatcher verifies (in
`app/services/events.py`):

1. the event type is supported and has a required capability mapped;
2. the plugin exists (`Plugin` row) and is enabled;
3. for workspace-scoped events, the event carries a valid `workspace_id`, the
   plugin is installed (`PluginInstallation`) and enabled in that workspace,
   and the emitting user is authorized for the workspace;
4. the plugin holds the capability required by `EVENT_CAPABILITY_MAP` through
   an explicit `CapabilityGrant` for the event's workspace.

If any condition cannot be proven, the handler is **denied** (recorded in the
dispatch result as a denial) and never invoked. `EVENT_CAPABILITY_MAP`
associates each event with the capability required to receive it, for example:

- `project.created` / `project.updated` / `project.deleted` → `PROJECT_READ`
- `workspace.member_added` / `workspace.member_removed` → `WORKSPACE_READ`
- `ai.analysis.completed` → `AI_ACCESS`
- `stellar.analysis.completed` / `stellar.network.detected` → `STELLAR_READ`
- `github.connected` / `github.disconnected` → `GITHUB_READ` (global events,
  delivered to enabled plugins without a per-workspace grant, since grants are
  workspace-scoped)

Subscribers registered *without* a `plugin_id` are internal handlers (trusted
application code) and are exempt from plugin capability enforcement. See
`docs/security.md` for the full security model.

The application already emits events from real flows: project import/delete,
workspace member add/remove, AI analysis completion, Stellar analysis
completion, GitHub connection, and plugin lifecycle.

### Lifecycle hooks (`on_enable` / `on_disable` / `on_uninstall`)

A loaded plugin class may implement optional lifecycle methods
(`app/services/plugins.py`):

```python
class MyPlugin:
    def __init__(self, app=None, manifest=None):
        ...

    def on_enable(self): ...      # after the registry enables the plugin
    def on_disable(self): ...     # after the registry disables the plugin
    def on_uninstall(self): ...   # before the registry removes the plugin
```

`PluginRegistry.enable` / `disable` / `uninstall` invoke the matching hook for
plugins already loaded in-process. Hooks are **isolated and non-fatal**: an
absent hook is a no-op, and a raising hook is logged without preventing the
state change — enabling/disabling always lands in the new state and uninstall
always removes the plugin, so the registry never ends inconsistent. The
management API never loads or executes plugin code on its own.

Per-workspace lifecycle is also observable through the dispatcher events
`plugin.enabled`, `plugin.disabled`, and `plugin.uninstalled` (mapped to
`WORKSPACE_READ`; emitted only on real transitions). The API exposes
enable/disable and an owner-only `uninstall` action that revokes every
workspace capability grant and removes the workspace installation.

## Plugin management (API + UI)

### Operator CLI

`app/services/plugins_cli.py` registers a `flask plugins` command group that
acts as the **operator**, reading/writing the persisted `Plugin` rows (the same
store the management API and dispatch-time authorization use) through
`app/services/plugin_ops.py`:

```bash
flask plugins list                      # id, version, enabled state
flask plugins inspect <plugin_id>       # full manifest metadata + state
flask plugins enable <plugin_id>        # enable (operator scope)
flask plugins disable <plugin_id>       # disable (operator scope)
flask plugins install <local-path>      # register from a local manifest dir
```

Every command supports `--json` for stable, parseable output. Exit codes
distinguish success (`0`), unknown plugin (`1`), and validation/service errors
(`2`). Security rules: installs accept **only local filesystem paths** with a
validated `manifest.json` (URLs are refused), the CLI never grants
capabilities (they remain explicit and per-workspace), and it never loads or
executes plugin code.

The user-facing **API + UI** is workspace-scoped: the `plugins` blueprint
(`app/plugins/`) provides the management layer. Plugin state is
**workspace-scoped**: a plugin exists globally (a
`Plugin` row) while having a separate `PluginInstallation` per workspace, each
with its own enabled state and capability grants.

Authorization reuses the Phase 7 workspace role model: any workspace member may
view; only the workspace **owner** may manage (the `manage_plugins`
capability). Non-members get `404`; members who are not owners get `403` for
management operations.

| Method | Path | Purpose | Role |
| ------ | ---- | ------- | ---- |
| `GET`  | `/plugins/` | Management page (workspace selector) | any member |
| `GET`  | `/plugins/api/workspaces` | Workspaces the user can access | any member |
| `GET`  | `/plugins/api/workspaces/<id>/plugins` | List plugins + installation state | any member |
| `GET`  | `/plugins/api/workspaces/<id>/plugins/<plugin_id>` | Inspect one plugin | any member |
| `POST` | `/plugins/api/workspaces/<id>/plugins/install` | Install a trusted/local manifest | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/enable` | Enable the workspace installation | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/disable` | Disable the workspace installation | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/capabilities` | Explicitly grant/revoke capabilities (`{"grant": [...], "revoke": [...]}`) | owner |

### Installation (trusted/local only)

`POST .../plugins/install` accepts a manifest object (`{"manifest": {...}}`).
The manifest is fully validated (`PluginManifest.from_dict`); malformed
manifests are rejected with `400`. The plugin id is taken **only** from the
validated manifest — a client cannot override it (identity binding). Installing:

- creates or reuses the global `Plugin` row (a conflicting entry point for an
  existing id returns `409`);
- creates a `PluginInstallation` for the workspace (`409` if already
  installed there);
- never loads or executes plugin code;
- never creates capability grants.

Capabilities granted through the API must be declared in the plugin's manifest
(declared vs granted are kept distinct). The `manage_plugins` role gate is the
only way to grant/revoke through the UI; grants are never implicit.

### Enable / disable

`enable`/`disable` operate on the **workspace installation**
(`PluginInstallation.enabled`), never on the global `Plugin` record, and never
create or remove capability grants. A disabled installation is denied by the
dispatch-time enforcement (see above), so disabled plugins cannot execute.
Re-enabling restores delivery while the grant remains valid.

### Per-workspace configuration

Each `PluginInstallation` carries a workspace-scoped `config` (JSON) that
installations read/write through owner-only endpoints:

- `GET .../plugins/<plugin_id>/config` — read the workspace configuration.
- `PUT .../plugins/<plugin_id>/config` with `{"config": {...}}` — replace it.

The stored config is seeded from the manifest's `configuration` at install.
Updates are validated (`app/services/plugin_config.py`) against that declared
configuration when one is present: unknown keys and wrong-typed values are
rejected with `400`, and every stored object is bounded (key count, nesting
depth, serialized size). Because config may hold secrets it is owner-only and
is deliberately **omitted** from the plugin list/inspect surfaces.

### Structured error reports

Plugin failures are recorded as bounded, safe rows (`app/services/plugin_errors.py`
→ `plugin_error_reports`) instead of only log lines. Each report stores the
plugin id, the operation (`dispatch:<event_type>`, `hook:<hook>`, …), the
exception type, and a length-bounded message. **No stack traces, event
payloads, or secrets are stored by default.**

Recording happens automatically when a plugin dispatch handler raises and when
a lifecycle hook fails inside an app context; it is best-effort and never
changes the existing failure-isolation behavior. Reports are read back
owner-scoped via `GET /plugins/api/workspaces/<ws>/plugin-errors` (owner only,
workspace-isolated) and operator-wide via `flask plugins errors [--json]`.

## Audit trail (capabilities & state)

Security-relevant plugin actions are recorded in the shared, append-only
workspace audit log (`ActivityEvent`, via `app/services/plugin_audit.py`):

- `plugin.capability.granted` / `plugin.capability.revoked` — explicit grant or
  revoke through the management API.
- `plugin.enabled` / `plugin.disabled` — workspace installation state changes.
- `plugin.denied` — a rejected capability request (e.g. not declared by the
  manifest) or a workspace-scoped dispatch-time denial (plugin disabled /
  not installed / missing capability grant).

Each entry carries only safe facts — actor, workspace, `plugin_id`, capability
name, action, outcome, a static reason, and (for dispatch denials) the refused
event type. **Secrets and event payloads are never stored.** Grant/revoke rows
are only appended on an actual change (repeat grants are not duplicated), and
dispatch-time denial recording is best-effort and never changes the fail-closed
delivery decision.

Entries are owner-visible through the workspace audit view (`GET
/workspaces/api/workspaces/<id>/audit`); the member activity feed always
excludes this audit subset, and cross-workspace records are never readable
outside the workspace they belong to (non-members receive 404).

## Writing a plugin

```python
# plugins/stellar_tools/plugin.py
class StellarPlugin:
    def __init__(self, app=None, manifest=None):
        self.app = app
        self.manifest = manifest

    def on_event(self, event):
        # subscribe via:
        #   from app.services.events import get_dispatcher
        #   get_dispatcher().subscribe("project.created", self.on_event, plugin_id="stellar-tools")
        ...
```

The `entry_point` value names `module.path:ClassName`. The plugin class may be
instantiated with `app` and `manifest` kwargs (see `Plugin.get_instance`).

## Security

See [security.md](security.md) for the full security model. In short:

- Capabilities are explicit and audited (never implicit).
- Plugin handler failures are isolated from the request lifecycle.
- Manifests are validated (no arbitrary code identifiers, no unknown
  capabilities).
- Event data carries workspace/user context so plugins can scope work; the
  Stellar analysis path reuses the Phase 7 content-access gate and fails closed.

## Contributing

The foundation is intentionally small. Planned, contributor-friendly work is
tracked under the **Phase 8 - Plugins & Extensions** milestone (label
`phase-8`), for example:

- Dependency resolution and version compatibility checks.
- A plugin development guide and an example plugin.
- CLI commands for plugin management.
- A capability audit trail (implemented).

## Testing

Run the whole suite with `pytest` (no external services; the event dispatcher
is reset between tests):

- `tests/test_plugins_manifest.py`, `tests/test_capabilities.py` — unit tests
  for manifest validation, capability grants, and role mappings.
- `tests/test_plugins_api.py` — the workspace-scoped plugin management API
  (install/enable/disable/grant) with authorization fail-closed and workspace
  isolation.
- `tests/test_plugin_audit.py` — the capability/state audit trail.
- `tests/test_event_authorization.py` — dispatch-time capability enforcement.
- `tests/test_event_wiring.py` — real routes emit events to authorized
  installed+granted plugins.
- `tests/test_plugin_integration.py` — the end-to-end flow: a manifest written
  to a temp directory is parsed/validated, installed through the real API
  (configuration stored, **no** implicit grant), the capability is granted
  explicitly, a handler subscribes to `project.created`, a real request emits
  the event, and the authorized handler runs and persists a result. It also
  proves a failing handler is isolated and that an un-granted (or disabled)
  plugin never executes.
