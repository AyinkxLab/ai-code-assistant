# Plugin developer guide

A step-by-step guide to shipping a plugin for the AI Code Assistant. It assumes
no prior knowledge of the plugin system; reading
[`plugins.md`](plugins.md) afterwards gives the full architecture (manifest
validation, capabilities, the event dispatcher, audit, and error reporting).

By the end you will have a working plugin installed and enabled in a workspace,
in well under 15 minutes. The complete example lives at
[`examples/plugins/hello_world/`](../examples/plugins/hello_world/).

---

## 0. Prerequisites

- The repository checked out and dependencies installed
  (`pip install -r requirements-dev.txt`).
- A configured app (`cp .env.example .env`, then `flask --app wsgi init-db`).
- A workspace you own (plugins are enabled per workspace).

## 1. Directory layout

Create your plugin's own directory. The only required file is `manifest.json`;
put the code next to it.

```text
my_plugin/
├── manifest.json     # required: identity, entry point, capabilities
├── plugin.py         # your plugin class
└── README.md         # optional
```

Discovery is **local-only**: an operator points `flask plugins install` at a
filesystem path. There is no remote/URL installation and the management API
never loads or executes plugin code by itself.

## 2. Author the manifest

`manifest.json` is validated against
[`plugins/plugin.schema.json`](../plugins/plugin.schema.json) and by
`PluginManifest.from_dict`. A minimal manifest:

```json
{
  "id": "hello-world",
  "name": "Hello World",
  "version": "0.1.0",
  "description": "Minimal example plugin that records project.created events.",
  "author": "Your Name",
  "entry_point": "hello_world.plugin:HelloWorldPlugin",
  "compatibility": ">=0.1.0",
  "capabilities": ["PROJECT_READ"],
  "configuration": {
    "greeting": "Hello from the hello-world plugin"
  }
}
```

Field rules (all required unless noted):

| Field | Rules |
| ----- | ----- |
| `id` | `^[a-z][a-z0-9_-]*$`, unique. The id is taken from the manifest — a client can never override it. |
| `name` / `description` / `author` | Non-empty human-readable strings. |
| `version` | Semantic version (`0.1.0`, `1.0.0-alpha`, …). |
| `entry_point` | `module.path:ClassName` (the class is instantiated with `app`/`manifest` kwargs). |
| `capabilities` | Non-empty list of **known** capabilities (see step 5). Declared ≠ granted. |
| `compatibility` | Optional PEP 440 specifier (`>=0.8.0`, `>=0.1.0,<0.5.0`). Omit for any version; an incompatible range is refused at install. |
| `configuration` | Optional default config object, seeded into each workspace installation. Never put secrets in the manifest. |

Unknown capabilities, a bad id/version/entry point, or an invalid compatibility
range raise `ManifestValidationError` and the install is rejected.

## 3. Write the plugin class

The `entry_point` names a class. It may implement an optional event handler and
the lifecycle hooks `on_enable` / `on_disable` / `on_uninstall`:

```python
from app.services.events import get_dispatcher

PLUGIN_ID = "hello-world"
EVENT_TYPE = "project.created"


class HelloWorldPlugin:
    def __init__(self, app=None, manifest=None):
        self.app = app
        self.manifest = manifest

    def on_event(self, event):
        # Keep handlers cheap and non-raising; return safe data if useful.
        return {"event": event.event_type, "project_id": (event.data or {}).get("project_id")}

    def on_enable(self):
        get_dispatcher().subscribe(EVENT_TYPE, self.on_event, plugin_id=PLUGIN_ID)

    def on_disable(self):
        get_dispatcher().unsubscribe(EVENT_TYPE, self.on_event)
```

Key points:

- **Subscribe with your `plugin_id`.** That is what makes dispatch-time
  capability enforcement apply (and what the audit log records). Subscribing
  without a `plugin_id` marks the handler as trusted internal code.
- **Only subscribe to supported events.** `subscribe` raises `EventError` for an
  unknown event type; the supported list is in `SUPPORTED_EVENTS`
  (`app/services/events.py`) and summarised in [`plugins.md`](plugins.md).
- **Never let a handler raise.** Handler failures are isolated (the request is
  never crashed) and recorded as a bounded error report, but a plugin that
  silently fails helps nobody.
- **Hooks are isolated too.** An absent hook is a no-op and a raising hook does
  not prevent the enable/disable state change.

## 4. Install and enable

Install the plugin (operator scope — registers the global `Plugin` row, never
grants capabilities, never executes code):

```bash
flask plugins install examples/plugins/hello_world
flask plugins inspect hello-world
```

Enable it **per workspace** through the management API or UI (owner only):

```bash
# List the workspaces you can access
curl -s "$BASE/plugins/api/workspaces" -H "Cookie: session=…"

# Install the manifest into a workspace (owner only)
curl -s -X POST "$BASE/plugins/api/workspaces/$WS/plugins/install" \
  -H "Content-Type: application/json" \
  -d '{"manifest": { ...same manifest object... }}'

# Enable the workspace installation
curl -s -X POST "$BASE/plugins/api/workspaces/$WS/plugins/hello-world/enable"
```

The management UI at `/plugins/` provides the same actions. Non-members get
`404`; members who are not the owner get `403` for management operations.

## 5. Grant capabilities (explicitly)

Capabilities are **never** granted implicitly. The manifest *declares* what the
plugin needs; a workspace owner must *grant* each capability for the workspace.
Granting a capability the manifest did not declare is rejected.

```bash
curl -s -X POST "$BASE/plugins/api/workspaces/$WS/plugins/hello-world/capabilities" \
  -H "Content-Type: application/json" \
  -d '{"grant": ["PROJECT_READ"], "revoke": []}'
```

Until the grant exists, the plugin is installed and enabled but its handlers are
**denied** at dispatch time (fail closed) and the denial is recorded in the
audit log. The capability list is in `app/services/capabilities.py`.

## 6. Verify

- `flask plugins list` / `flask plugins inspect hello-world` — registration and
  enabled state.
- `GET /plugins/api/workspaces/<ws>/plugins` — per-workspace installation state.
- Trigger a real event (for `hello-world`, import/create a project) and confirm
  the handler ran.
- `flask plugins errors` or `GET /plugins/api/workspaces/<ws>/plugin-errors` —
  bounded error reports if a handler or hook raised.
- The workspace audit view (`GET /workspaces/api/workspaces/<id>/audit`) records
  grant/revoke, enable/disable, and dispatch denials.

## 7. Common pitfalls

- **Capability not granted.** The most common cause of "my handler never runs".
  Install + enable is not enough; grant the declared capability.
- **Wrong `entry_point`.** It must be an importable `module.path:ClassName`. For
  local development, put the plugin's parent directory on `PYTHONPATH`
  (`export PYTHONPATH="$PWD/examples/plugins:$PYTHONPATH"`).
- **Subscribing without `plugin_id`.** This bypasses capability enforcement and
  should only be used by trusted application code, not plugins.
- **Unsupported event type.** `subscribe` raises `EventError`; check
  `SUPPORTED_EVENTS`.
- **Secrets in config.** Workspace config may hold secrets, but it is
  owner-only and omitted from list/inspect surfaces. Never commit secrets to the
  manifest.
- **Expecting auto-discovery.** There is none at startup; installs are explicit
  and local.

## 8. Hello World, end to end

The example plugin ties it all together:

1. Read [`examples/plugins/hello_world/manifest.json`](../examples/plugins/hello_world/manifest.json)
   — id `hello-world`, entry point `hello_world.plugin:HelloWorldPlugin`,
   declared `PROJECT_READ`.
2. Read [`examples/plugins/hello_world/plugin.py`](../examples/plugins/hello_world/plugin.py)
   — `HelloWorldPlugin` subscribes to `project.created` in `on_enable` and
   records the project id.
3. Register and enable it (steps 4–5), grant `PROJECT_READ`, then create a
   project in that workspace. The handler runs and returns the project id.

`tests/test_plugin_example.py` validates the example manifest and class so the
guide cannot drift from a working plugin.

## See also

- [`plugins.md`](plugins.md) — full architecture and management API reference.
- [`plugins/plugin.schema.json`](../plugins/plugin.schema.json) — JSON Schema.
- [`../plugins.md`](plugins.md#security) / [`security.md`](security.md) — the
  security model (capabilities, fail-closed dispatch, error isolation).
