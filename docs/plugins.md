# Plugin System

The AI Code Assistant ships an extensible plugin architecture so that Stellar,
Soroban, GitHub, AI-provider, and developer-tooling features can be added
without modifying the core application.

> **Status:** Phase 8 foundation. Manifest validation, the registry, the
> capability model, and the event/hook system are implemented and tested. A
> plugin management UI, dependency resolution, and a marketplace are **not**
> implemented yet — see [Contributing](#contributing).

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

Validation errors raise `ManifestValidationError`; unreadable or malformed
manifest files raise `PluginError`.

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

The application already emits events from real flows: project import/delete,
workspace member add/remove, AI analysis completion, Stellar analysis
completion, and GitHub connection.

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

- Plugin management API/UI (install, enable/disable, configure per workspace).
- Dependency resolution and version compatibility checks.
- Per-event capability enforcement (require `STELLAR_READ` before delivering
  `stellar.*` data).
- A plugin development guide and an example plugin.
- CLI commands for plugin management.
