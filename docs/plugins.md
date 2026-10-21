# Plugin System

The AI Code Assistant ships an extensible plugin architecture so that Stellar,
Soroban, GitHub, AI-provider, and developer-tooling features can be added
without modifying the core application.

> **Status:** Phase 8 + Phase 9 security foundations. Manifest validation, the
> registry, the capability model, the event system with fail-closed
> dispatch-time capability enforcement, a workspace-scoped plugin management API
> + UI, publisher trust (trusted keys, embedded and detached Ed25519
> signatures), catalog-pinned remote installation, the plugin lock-entry model,
> a member-readable marketplace browser (install/update/rollback for owners),
> transitive catalog dependency validation, and the published marketplace JSON
> schema are implemented and tested. Artifact download/execution remains out of
> scope — installs register validated metadata only.

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
- **Updates are gated too**: `POST .../plugins/<plugin_id>/update` refuses a
  manifest whose range excludes the running version *before* writing anything,
  so an incompatible release can never replace a working installation.
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

### Execution timeout (#408)

Plugin event handlers and lifecycle hooks run with a bounded execution budget
(`app/services/plugin_execution.py`). The budget is
`PLUGIN_EXECUTION_TIMEOUT` seconds (default `5`; `0` or negative disables it):

- the callback runs in a short-lived daemon thread while the request waits;
- if it overruns, the dispatcher **stops waiting**, records one bounded error
  report (`plugin_error_reports`, operation `dispatch:<event>` or
  `hook:<hook>`), and continues with the next handler or lifecycle step;
- state changes still land: enabling/disabling always completes, and dispatch
  keeps running the remaining subscribers.

Python cannot interrupt a thread, so an over-budget callback is **abandoned**
rather than killed. It may still finish in the background, which is why plugin
code must be idempotent and must not depend on being stopped.

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
flask plugins publishers                # list trusted publisher ids (never keys)
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
| `GET`  | `/plugins/api/workspaces/<id>/catalog` (alias `/marketplace`) | Browse catalog metadata, trust, dependencies and install/update/rollback state | any member |
| `GET`  | `/plugins/api/workspaces/<id>/lockfile` | Read the resolved lockfile pin set | any member |
| `GET`  | `/plugins/api/trusted-publishers` | List configured trusted publisher ids (never keys) and the active policy | authenticated |
| `POST` | `/plugins/api/workspaces/<id>/plugins/install` | Install a trusted/local manifest | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/install-from-catalog` | Install a catalog-pinned plugin (trust policy + dependency graph enforced) | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/update` | Update a registered plugin from a manifest (compatibility-gated) | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/update-from-catalog` | Atomically update from a catalog version | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/rollback` | Roll back to the lockfile/catalog target | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/enable` | Enable the workspace installation | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/disable` | Disable the workspace installation | owner |
| `POST` | `/plugins/api/workspaces/<id>/plugins/<plugin_id>/capabilities` | Explicitly grant/revoke capabilities (`{"grant": [...], "revoke": [...]}`) | owner |

### Installation (local manifests or signed catalog entries)

Local manifests are the default. Remote installation is supported only through
the operator-controlled catalog described in
[Remote install from the validated catalog](#remote-install-from-the-validated-catalog-389);
the API never accepts a client-supplied artifact URL.

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

### Publisher trust: keys and detached signatures (#385, #386)

Operators list trusted publishers in `PLUGIN_TRUSTED_KEYS`, a JSON object that
maps a publisher id (lowercase `[a-z0-9._-]`, at most 64 characters) to an
Ed25519 public key. Keys may be base64-encoded raw 32-byte keys or PEM
`SubjectPublicKeyInfo` text:

```bash
export PLUGIN_TRUSTED_KEYS='{"acme-labs": "<base64 or PEM Ed25519 public key>"}'
export PLUGIN_TRUST_POLICY=required   # or if-present (default)
```

Key material is validated when the application starts
(`app.services.plugin_trust.validate_trust_config`): a malformed key, a
non-canonical publisher id, or an unknown policy aborts startup with a clear
message instead of silently treating every signature as invalid. Keys are
never returned by the API or the CLI — inspect the configured ids with:

```bash
flask plugins publishers             # human-readable ids + policy
flask plugins publishers --json      # {"publishers": [...], "count": N, "policy": ...}
```

`GET /plugins/api/trusted-publishers` exposes the same read-only view (any
authenticated user): publisher ids, a count, and the active policy, never key
bytes or fingerprints.

A publisher signs the **canonical manifest body**: compact, key-sorted JSON with
`signature`, `trust_state`, and `trust_publisher` removed. The signature is a
base64 Ed25519 signature over those bytes and may be:

- **embedded** in the manifest (`"signature": {"publisher", "algorithm":
  "ed25519", "value"}`), or
- **detached**, delivered next to the manifest (a `.sig` file or the catalog
  entry's `signature` field).

`app/services/plugin_signatures.py` is the single verification path for both
forms. Policy handling is deliberately strict:

| Situation | `if-present` | `required` |
| --------- | ------------ | ---------- |
| Valid signature from a configured publisher | accepted (`Trusted`) | accepted (`Trusted`) |
| No signature | accepted (`Unverified`) | refused |
| Forged/malformed signature, or unknown publisher | **refused** | **refused** |

Tolerating absence must never mean tolerating forgeries, so an invalid
signature fails even in `if-present` mode. Local-path installs keep working
under the default policy; set `required` to demand signatures everywhere.

### Remote install from the validated catalog (#389)

`POST /plugins/api/workspaces/<id>/plugins/install-from-catalog` installs a
plugin that an operator has already published in the catalog. The request only
names the entry — a client can never supply a manifest, an artifact URL, or a
key:

```bash
curl -X POST https://<host>/plugins/api/workspaces/7/plugins/install-from-catalog \
  -H "Content-Type: application/json" \
  -d '{"plugin_id": "hello-world", "version": "0.1.0"}'
```

`version` is optional; without it the newest catalogued version wins (PEP 440
ordering). `PLUGIN_CATALOG_FILE` points at the catalog JSON (abridged below;
`manifest` is a complete, schema-valid manifest):

```json
{
  "version": 1,
  "plugins": [
    {
      "id": "hello-world",
      "version": "0.1.0",
      "source": "https://plugins.example.com/hello-world-0.1.0.zip",
      "integrity": "sha256:<64 lowercase hex characters>",
      "signature": {
        "publisher": "acme-labs",
        "algorithm": "ed25519",
        "value": "<base64 signature over the manifest>"
      },
      "manifest": { "id": "hello-world", "...": "see plugins/plugin.schema.json" }
    }
  ]
}
```

The endpoint is owner-only (`manage_plugins`) and fails closed:

| Failure | Status |
| ------- | ------ |
| Missing/invalid `plugin_id` or `version` | `400` |
| No catalog configured, or unknown id/version | `404` |
| Invalid manifest or incompatible `compatibility` range | `400` |
| Trust refusal (unsigned under `required`, forged, unknown publisher) | `403` |
| Already installed, or entry point conflicts with a registered plugin | `409` |
| Non-member of the workspace | `404` |
| Member who is not the owner | `403` |

On success it registers metadata only: the global `Plugin` row and the
workspace `PluginInstallation`. The response also reports the catalog `source`
and `integrity` so the resolved pin is visible. It does **not** download the
artifact, execute plugin code, or create capability grants, and trust refusals
are appended to the workspace audit trail.

### Marketplace trust model (end to end)

The operator-facing summary lives in
[the security model](security.md#marketplace-trust-model). In short: the
catalog source is allowlisted, every entry is schema/integrity/signature
checked, installs are owner-only and dependency-validated, updates are atomic
with rollback, and the lockfile pins the resolved set. Catalog data is
advertised metadata only — it never executes and never grants a capability.

| Concern | Implementation | Tests |
| ------- | -------------- | ----- |
| Catalog parsing, id/version/source/integrity validation | `app/services/plugin_catalog.py` | `tests/test_plugin_catalog_install.py`, `tests/test_plugin_catalog_remote.py`, `tests/test_plugin_catalog_deps.py` |
| Read-only discovery + manifest-schema validation | `app/services/marketplace.py`, `app/plugins/routes.py` | `tests/test_marketplace_catalog.py` |
| Allowlist / HTTPS / timeout / size / no redirects | `app/services/plugin_network.py` | `tests/test_plugin_network.py` |
| Ed25519 signatures and trust policy | `app/services/plugin_signatures.py`, `app/services/plugin_trust.py` | `tests/test_plugin_signatures.py`, `tests/test_plugin_trust.py` |
| Transitive dependency resolution and cycles | `app/services/plugin_deps.py` | `tests/test_plugin_catalog_deps.py`, `tests/test_plugin_deps.py` |
| Atomic update / rollback | `app/services/plugin_ops.py`, `app/services/plugin_updates.py` | `tests/test_plugin_update.py`, `tests/test_marketplace_e2e.py` |
| Lockfile pinning and serialization | `app/models/plugin*.py`, `app/services/plugin_lockfile.py` | `tests/test_plugin_lock_entries.py`, `tests/test_plugin_lockfile.py` |

Configuration for each layer is in the
[configuration reference](#configuration-reference); remote installation stays
disabled until an operator sets a catalog and allowlist.

### Marketplace catalog schema (#373)

[`plugins/marketplace.schema.json`](../plugins/marketplace.schema.json) is a
Draft-07 schema for marketplace entries (id, name, version, description,
author, capabilities, permissions, dependencies, compatibility, artifact URL,
sha256 and publisher key id). It also accepts the installer catalog shape
(`id`/`version`/`source`/`integrity`/`manifest`), so existing operator fixtures
continue to validate without changing `parse_catalog`. The reusable helper
`app.services.marketplace_schema.validate_marketplace_entry` loads the schema
and returns path-qualified errors via the dependency-free draft-07 subset
validator in `app/services/schema_validation.py` (shared with the manifest
schema tests). No new runtime dependency is required.

### Plugin lock entries (#394)

`PluginLockEntry` (`plugin_lock_entries`) persists the resolved plugin set for
reproducible installs: plugin id, pinned version, source, sha256 integrity
hash, declared dependencies, the workspace, and its owner. A row is unique per
`(workspace_id, plugin_id)`, and lock entries never imply capability grants.

Rows serialize through the existing canonical lockfile builder, so
`dumps_lockfile([*rows])` produces the same byte-stable document as any other
resolved entry and omits volatile audit fields (`resolved_at`, row/workspace
ids):

```python
from app.models import PluginLockEntry
from app.services.plugin_lockfile import dumps_lockfile

rows = PluginLockEntry.query.filter_by(workspace_id=workspace_id).all()
print(dumps_lockfile(rows))
```

The Alembic migration for the table is tracked by the Phase 9 lockfile
migration issue; the model and its serialization are covered by
`tests/test_plugin_lock_entries.py`.

The workspace plugin page renders this lockfile read-only for every workspace
member (#364): each pinned plugin row shows its id, version, source, integrity
hash, and dependencies. Any member reads it from
`GET /plugins/api/workspaces/<id>/lockfile`; an empty lockfile renders an
explicit empty state instead of a blank panel. Configuration values are never
part of the lockfile payload, and source URLs have embedded credentials
stripped before rendering, so the viewer cannot expose a secret. Mutating
plugin routes remain owner-only.

### Marketplace browser (#364)

`GET /plugins/api/workspaces/<id>/catalog` (also available as
`/marketplace`) is readable by any workspace member and returns catalog entries
with stable, bounded pagination (`page` >= 1, `limit` clamped to 100,
deterministic order by plugin id then version). Each entry exposes safe
metadata only: name/version/description/author, declared capabilities and
permissions, dependency requirements, compatibility, trust state and publisher,
artifact source and sha256 integrity, plus per-workspace `installed`,
`update_available`, `rollback_available` and `actions` fields. `can_manage` and
every action are true only for owners; the backend independently enforces
`manage_plugins` on install/update/rollback.

The management page (`app/templates/plugins/index.html` +
`app/static/js/plugin_catalog.js`) renders this browser with trust and
compatibility badges, dependency/artifact details, loading, empty, error and
timeout states (shared with `plugin_marketplace.js`), and owner-only
install/update/rollback buttons with surfaced errors. Catalog text is escaped
before rendering.

### Marketplace discovery API (#360)

The read-only discovery API advertises catalog entries without installing
anything. It is separate from the per-workspace installer catalog above:

| Route | Access | Description |
| ----- | ------ | ----------- |
| `GET /plugins/api/marketplace` | any signed-in workspace member | Paginated list with optional `q` (id/name/description/author), `capability`, and `workspace_id` filters. |
| `GET /plugins/api/marketplace/<id>` | any signed-in workspace member | One entry, newest version by default or `?version=` pinned. |

`app/services/marketplace.py` loads the catalog from
`PLUGIN_MARKETPLACE_CATALOG` (default `plugins/marketplace.json`, which ships
empty so the offline default is safe). If `PLUGIN_MARKETPLACE_URL` is set and no
local catalog is configured, the document is fetched through
`app/services/plugin_network.py`: HTTPS only, allowlisted host, bounded timeout
and body size, and redirects refused.

The discovery API uses the lenient loader introduced in #374/#376: a missing or
unreadable local file yields an empty catalog (HTTP 200, never an error), and
individual invalid entries are excluded with bounded warnings in the response
instead of hiding every valid entry. A malformed JSON document or wrong catalog
shape is still a `400` so operators can fix it. The installer catalog used by
`install-from-catalog` remains strict and rejects the whole document. Every
advertised manifest is validated against
[`plugins/plugin.schema.json`](../plugins/plugin.schema.json).

Each returned entry contains only `id`, `name`, `version`, `description`,
`author`, `capabilities`, `permissions`, `dependencies`, `compatibility`,
`integrity`, and `source` (plus `signature` when present). Catalog data is
untrusted metadata: it is never executed and never grants capabilities.
Pagination is bounded (`page >= 1`, `limit` clamped to 100) and ordered by id
then version. Tests live in `tests/test_marketplace_catalog.py`,
`tests/test_marketplace_lenient.py`, and `tests/test_marketplace_performance.py`.

### Catalog dependency validation (#391)

Before a catalog install writes anything, `CatalogDependencyGraph`
(`app/services/plugin_deps.py`) walks the target entry's manifest dependencies
recursively through the catalog. It collapses duplicate edges/nodes, returns a
deterministic dependency-first order, and reports a full cycle chain such as
`a -> b -> a`. Non-catalog dependencies are not required to be installed before
install; the existing enable-time resolver remains authoritative for their
installed/disabled/version state. Both the API route and the service/CLI path
(`install_from_catalog`) run this validation.

All marketplace surfaces (plugin list, lockfile, and detail actions) share the
empty/error/timeout states in `app/static/js/plugin_marketplace.js` (#421).
Failures show friendly copy and a **Retry** button, and requests are
timeout-bounded so a stalled marketplace never leaves the UI loading forever.
The state renderers are covered by
`tests/js/plugin_marketplace.test.js`.

### Updating a plugin

`POST .../plugins/<plugin_id>/update` accepts the same `{"manifest": {...}}`
shape as install, but applies it to an already-registered plugin. The manifest
is validated and identity-bound:

- `id` must equal the plugin being updated;
- `entry_point` must not change (`409`), so an update can never point an
  installed plugin at different code;
- the declared PEP 440 `compatibility` range is enforced against the running
  application version **before** anything is written. An incompatible range is
  refused with `400` and a message naming both the range and the current
  version; the registered plugin and its workspace installations are left
  untouched.

Updates change the plugin's metadata (version, description, capabilities,
dependencies, configuration defaults, trust fields) but never capability grants
or per-workspace configuration, which stay explicit and workspace-scoped. The
plugin must be installed in the authorizing workspace (`404` otherwise).

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

Recording happens automatically at every plugin invocation boundary inside an
app context: **loading** an entry point (`load`), **instantiating** a plugin
class, running a **lifecycle hook** (`hook:<hook>`), **dispatching** an event
handler (`dispatch:<event_type>`), and the legacy registry dispatcher
(`registry-dispatch:<event_type>`). It is best-effort and never changes the
existing failure-isolation behavior. Reports are read back owner-scoped via
`GET /plugins/api/workspaces/<ws>/plugin-errors` (owner only,
workspace-isolated) and operator-wide via `flask plugins errors [--json]`.

## Configuration reference

Marketplace and plugin settings are environment variables. The defaults are
deliberately fail-closed: catalog installation is disabled, no publisher is
trusted, and plugin outbound traffic is denied until an operator opts in.

| Variable | Safe default | Purpose |
| -------- | ------------ | ------- |
| `PLUGIN_CATALOG_FILE` | empty | Path to the validated JSON catalog used by `POST .../plugins/install-from-catalog`. Empty disables catalog installation. |
| `PLUGIN_MARKETPLACE_ENABLED` | `1` | Enables the read-only discovery API (`GET /plugins/api/marketplace`). Disabling returns 404. |
| `PLUGIN_MARKETPLACE_CATALOG` | `plugins/marketplace.json` | Local JSON catalog for the discovery API. Ships empty; set it to advertise validated entries. |
| `PLUGIN_MARKETPLACE_URL` | empty | Optional https catalog URL fetched through the bounded plugin transport when no local marketplace catalog is configured. The host must be on `PLUGIN_NETWORK_ALLOWLIST`; redirects, non-https URLs, oversized bodies, and non-allowlisted hosts are refused before any request. |
| `PLUGIN_DISCOVERY_DIR` | `plugins` | Read-only directory scanned for `manifest.json` files by `GET /plugins/discover`. Nothing is installed or executed. |
| `PLUGIN_TRUST_POLICY` | `if-present` | `if-present` accepts unsigned manifests but always refuses forged or untrusted signatures; `required` also refuses unsigned manifests. Use `required` for a hosted marketplace. |
| `PLUGIN_TRUSTED_KEYS` | `{}` | JSON object of canonical publisher ids to Ed25519 public keys (base64 raw 32 bytes or PEM). Invalid key material aborts startup. |
| `PLUGIN_NETWORK_ALLOWLIST` | empty | Comma-separated outbound hosts. Empty denies every plugin request; `*` allows any public host and `*.example.com` allows subdomains. |
| `PLUGIN_NETWORK_HTTPS_ONLY` | `1` | Rejects non-HTTPS plugin requests. Keep enabled outside isolated development. |
| `PLUGIN_NETWORK_ALLOW_PRIVATE` | `0` | Allows private/loopback/link-local targets. Keep disabled in production. |
| `PLUGIN_NETWORK_TIMEOUT` | `15` | Outbound request timeout in seconds. |
| `PLUGIN_NETWORK_MAX_BYTES` | `2097152` | Maximum response body accepted from a plugin request (2 MiB). |
| `PLUGIN_NETWORK_STRICT_DNS` | `0` | When enabled, an unresolvable host is rejected instead of tolerated. |
| `PLUGIN_EXECUTION_TIMEOUT` | `5` | Seconds a plugin event handler or lifecycle hook may run before it is abandoned and recorded as a bounded error. `0` or negative disables the bound. |
| `PLUGIN_CONFIG_MAX_BYTES` | `65536` | Maximum serialized size of a stored per-workspace plugin configuration. |
| `PLUGIN_CONFIG_MAX_KEYS` | `200` | Maximum number of top-level keys in a stored plugin configuration. |
| `PLUGIN_CONFIG_MAX_DEPTH` | `8` | Maximum nesting depth of a stored plugin configuration. |
| `PLUGIN_ERROR_REPORT_LIMIT` | `100` | Maximum error reports returned to a workspace owner (hard ceiling `500`). |

**Catalog cache:** there is no active cache TTL setting yet. The catalog file is
validated and re-read for each install request, so an operator's catalog edit is
visible immediately. A TTL cache and its `PLUGIN_MARKETPLACE_CACHE_TTL` setting
are tracked separately in #377.

**Remote catalog (#375):** when `PLUGIN_CATALOG_FILE` is empty and
`PLUGIN_MARKETPLACE_URL` is set, the catalog is fetched through
`app/services/plugin_network.py`. The URL must be https and its host must be on
`PLUGIN_NETWORK_ALLOWLIST`; those checks run before any socket is opened.
Redirects are refused, the request uses `PLUGIN_NETWORK_TIMEOUT`, and the body
is capped at the catalog limit (1 MiB). A local `PLUGIN_CATALOG_FILE` always
takes precedence.

### Resource limits and failure behavior (#409, #410)

All plugin resource limits are configuration-driven
(`app/services/plugin_limits.py`) and have conservative defaults. Invalid or
non-positive values fall back to the default rather than disabling the bound;
the execution timeout is the one deliberate exception, where `0` or a negative
value disables the bound for local debugging. Defaults, the settings that
override them, and the failure behavior are summarized below:

| Boundary | Default bound | Behavior when exceeded |
| -------- | ------------- | ---------------------- |
| Event handler / lifecycle hook | `PLUGIN_EXECUTION_TIMEOUT=5` seconds | The callback is abandoned (daemon thread), one bounded error report is recorded, and dispatch continues with the remaining handlers. |
| Stored workspace configuration | 64 KiB, 200 keys, depth 8 | The write is rejected with `400` and nothing is stored. |
| Owner error-report read | 100 rows | The response is truncated to the configured limit (maximum 500). |
| Outbound plugin HTTP | 15 seconds, 2 MiB | The request fails closed; see the network settings below. |

Failure isolation covers every plugin invocation boundary: **loading** an
entry point, **instantiating** the plugin class, running a **lifecycle hook**,
and **dispatching** an event handler or legacy registry handler. A failure at
any boundary is contained, leaves the registry consistent, and records one
bounded report (`load`, `hook:<name>`, `dispatch:<event>`, or
`registry-dispatch:<event>`). Requests still complete and other handlers still
run. Regression tests inject failures at each boundary in
`tests/test_plugin_failure_isolation.py`.

Example production posture:

```bash
export PLUGIN_CATALOG_FILE=/etc/ai-code-assistant/catalog.json
export PLUGIN_TRUST_POLICY=required
export PLUGIN_TRUSTED_KEYS='{"acme-labs": "<base64 or PEM Ed25519 public key>"}'
export PLUGIN_NETWORK_ALLOWLIST=plugins.example.com
export PLUGIN_NETWORK_HTTPS_ONLY=1
export PLUGIN_NETWORK_ALLOW_PRIVATE=0
export PLUGIN_EXECUTION_TIMEOUT=5
export PLUGIN_CONFIG_MAX_BYTES=65536
export PLUGIN_CONFIG_MAX_KEYS=200
export PLUGIN_CONFIG_MAX_DEPTH=8
export PLUGIN_ERROR_REPORT_LIMIT=100
```

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

> **New to plugins?** Start with the step-by-step
> [Plugin developer guide](plugin-developer-guide.md). It walks through the
> layout, manifest, plugin class, install/enable, explicit capability grants,
> the local verify loop, and common pitfalls, using the runnable
> [`examples/plugins/hello_world`](../examples/plugins/hello_world) example.

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

## Example plugin

A minimal, runnable example lives at
[`examples/hello-plugin/`](../examples/hello-plugin):

- `manifest.json` — a valid manifest (id `hello-plugin`, capability
  `PROJECT_READ`, `entry_point` `plugin:HelloPlugin`).
- `plugin.py` — `HelloPlugin`, which subscribes to `project.created`, logs
  each event, and records it in `received`.

Run it from the repository root. Both the repository root (so `app` resolves)
and the example directory (so the `entry_point` resolves) must be importable:

```bash
# Windows (PowerShell)
$env:PYTHONPATH = ".;examples/hello-plugin"
python examples/hello-plugin/plugin.py

# Linux / macOS
PYTHONPATH=.:examples/hello-plugin python examples/hello-plugin/plugin.py
```

It registers through the real `PluginRegistry`, subscribes to
`project.created` via the shared `EventDispatcher`, and prints how many events
it recorded. Subscribing never requires a capability grant; grants are enforced
only when the host dispatches a live event (see [Security](#security)). The
manifest schema and the load/register flow are covered by
`tests/test_hello_plugin_example.py`.

### Stellar notifier example (#415)

[`examples/plugins/stellar_notifier/`](../examples/plugins/stellar_notifier)
shows an event-driven, read-only integration for Stellar analysis:

- declares `STELLAR_READ` and subscribes to `stellar.analysis.completed`;
- formats a one-line summary (`Project 7 — Stellar detected, confidence 0.98`)
  that is capped at `MAX_SUMMARY_CHARS`, with each field truncated and the
  in-memory history bounded to `MAX_RECENT_SUMMARIES`;
- copies the payload instead of mutating it and makes **no network or database
  calls**, so it runs offline under the mock network;
- is validated end-to-end by
  `tests/test_stellar_notifier_example.py` (schema, load/register, event path,
  bounding, read-only behaviour, and an offline socket guard).

## Security

See [security.md](security.md) for the full security model. In short:

- Capabilities are explicit and audited (never implicit).
- Plugin handler failures are isolated from the request lifecycle.
- Manifests are validated (no arbitrary code identifiers, no unknown
  capabilities).
- Publisher signatures are Ed25519 over the canonical manifest body; forged or
  untrusted signatures are always refused, and `required` also refuses
  unsigned entries.
- Trusted publisher keys are validated at startup and never exposed by the API
  or CLI (only publisher ids are listed).
- Catalog installs are owner-only, resolve server-side, register metadata only,
  and never download code or grant capabilities.
- Event data carries workspace/user context so plugins can scope work; the
  Stellar analysis path reuses the Phase 7 content-access gate and fails closed.
- Event data carries workspace/user context so plugins can scope work; the
  Stellar analysis path reuses the Phase 7 content-access gate and fails closed.

## Contributing

The foundation is intentionally small. Planned, contributor-friendly work is
tracked under the **Phase 8 - Plugins & Extensions** milestone (label
`phase-8`), for example:

- Dependency resolution and version compatibility checks (implemented — see
  `app/services/plugin_deps.py` and `app/services/plugin_compat.py`).
- Plugin packaging/distribution and a marketplace (implemented — signed,
  catalog-pinned installs plus the read-only discovery API; see
  [Marketplace trust model](#marketplace-trust-model-end-to-end)).
- CLI commands for plugin management.
- A capability audit trail (implemented).
- A plugin development guide and an example plugin (implemented — see
  [plugin-developer-guide.md](plugin-developer-guide.md) and
  [`examples/plugins/hello_world`](../examples/plugins/hello_world)).

## Testing

Run the whole suite with `pytest` (no external services; the event dispatcher
is reset between tests):

- `tests/test_plugins_manifest.py`, `tests/test_capabilities.py` — unit tests
  for manifest validation, capability grants, and role mappings.
- `tests/test_plugins_api.py` — the workspace-scoped plugin management API
  (install/enable/disable/grant) with authorization fail-closed and workspace
  isolation.
- `tests/test_plugin_trust.py` — trusted publisher key loading (base64/PEM),
  startup rejection of bad configuration, and the secret-free listing surfaces.
- `tests/test_plugin_signatures.py` — canonical payloads, detached signatures,
  forged/unsigned handling, and the embedded-signature regression path.
- `tests/test_plugin_catalog_install.py` — catalog parsing/validation and the
  remote-install endpoint: authorization, trust policy, 4xx failure paths, and
  no implicit grants or code execution.
- `tests/test_marketplace_e2e.py` — offline catalog-to-dispatch coverage:
  install, explicit enable, dependency ordering, trust refusal, rollback, and
  no implicit grants.
- `tests/test_plugin_lock_entries.py` — the `PluginLockEntry` model, scoping,
  unique constraint, and canonical lockfile serialization.
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
