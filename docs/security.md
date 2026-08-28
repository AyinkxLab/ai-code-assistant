# Security model

This document records the security posture of the Phase 8 plugin and Stellar
architecture. It is the result of a review pass over the new code and should be
kept in sync as the plugin system grows.

## Principles

- **Fail closed.** Where authorization or safety is uncertain, the system
  denies.
- **Capabilities are explicit.** No plugin receives a capability implicitly.
- **Isolation.** A plugin handler failure can never crash a request.
- **No fabricated integrations.** The Stellar analysis only claims what the
  indexed files actually demonstrate.
- **Configuration over user input.** Network endpoints come from environment
  configuration, never from user or project data.

## Threat review

| Threat                          | Control                                                                                          | Status |
| ------------------------------- | ------------------------------------------------------------------------------------------------ | ------ |
| Plugin privilege escalation     | Manifests declare capabilities; runtime grants require an explicit `CapabilityGrant` per workspace (`CapabilityStore.grant`). Roles are mapped to capabilities in `ROLE_CAPABILITY_MAPPING`; `validate_plugin_capability` fails closed. | Implemented |
| Unauthorized plugin capabilities| Capabilities are validated against the `Capability` enum before granting; unknown strings are rejected. | Implemented |
| Workspace isolation             | Capability grants are scoped `(plugin_id, workspace_id, capability)` with a uniqueness constraint. | Implemented |
| Project isolation               | The Stellar AI analysis reuses `assert_content_access` (owner-only, fails closed). Project files are only ever read for projects the caller may access. | Implemented |
| Stellar data access             | `StellarService` is read-only; it never signs, sends, or funds. | Implemented |
| Secret exposure                 | `.env`/key files are skipped at project import; the Stellar prompt explicitly flags hard-coded credentials; service config uses env vars only; no secrets are stored or logged. | Implemented |
| Endpoint manipulation           | Endpoint URLs come from `current_app.config` only. | Implemented |
| SSRF                           | `validate_endpoint_url`: https-only for public networks, loopback-only for custom networks; requests are restricted to the configured Horizon base URL and bounded by timeout + response-size cap. | Implemented |
| Path traversal                  | Project file access already rejects traversal at import and in file/tree APIs; Stellar detection only reads `path`/`content` of already-indexed files. | Existing + verified |
| Malicious plugin metadata       | Manifest validation rejects bad ids, versions, entry points, unknown capabilities, and empty capability lists. | Implemented |
| Plugin management authorization | The `plugins` API is workspace-scoped: members may view, only the owner (`manage_plugins`) may install/enable/disable/grant. Non-members get 404, non-owners get 403; the server derives authorization from trusted workspace membership, never from client-supplied ownership. | Implemented |
| Plugin identity binding         | Installation binds the plugin to the id declared in the validated manifest; a conflicting entry point for an existing id is rejected (409). Install never loads or executes code and never auto-grants capabilities; grants are restricted to manifest-declared capabilities. | Implemented |
| Event authorization             | Before a plugin handler runs, the dispatcher verifies the event type is supported, the plugin exists and is enabled, and for workspace-scoped events the plugin is installed and enabled in the event's workspace, the emitting user is authorized for the workspace, and the plugin holds the capability required by `EVENT_CAPABILITY_MAP` via an explicit `CapabilityGrant` for that workspace. Unknown/missing/disabled/unauthorized cases are denied (fail closed); denials are recorded, never delivered. | Implemented |

## Testing

Security-relevant coverage includes:

- `tests/test_stellar_service.py` — endpoint validation, SSRF (out-of-base and
  non-loopback rejection), size caps, error taxonomy.
- `tests/test_stellar_analysis.py` — non-owner and unauthenticated users fail
  closed; non-Stellar projects are reported honestly.
- `tests/test_plugins_manifest.py` — malicious/invalid manifests rejected.
- `tests/test_capabilities.py` — capability grants are explicit, deduplicated,
  validated, and revoked correctly.
- `tests/test_events.py` / `tests/test_event_wiring.py` — handler failures are
  isolated and never break the request lifecycle.
- `tests/test_event_authorization.py` — dispatch-time capability enforcement:
  authorized delivery, denial for unknown/disabled/mis-granted plugins,
  cross-workspace and cross-plugin isolation, missing-context fail-closed,
  project/workspace consistency (confused-deputy defense), no auto-grant, and
  the Stellar/AI event capability requirements.
- `tests/test_project_import.py` — archive traversal/symlink/size guards.

## Known limitations / planned hardening

- Custom-network endpoints are restricted to loopback; a production operator
  using a remote custom node needs to extend the allow-list policy.
- Plugin code loading is available (`Plugin.load`) but not yet wired to any
  user-controlled install flow; a plugin install API must enforce capabilities
  and review before execution.
- Global events (`github.connected`, `github.disconnected`) carry no workspace
  context and are delivered to any enabled plugin that subscribes; capability
  grants are workspace-scoped, so a per-workspace grant check does not apply to
  them.
- Subscribers registered without a `plugin_id` are treated as trusted internal
  handlers and bypass plugin capability checks; plugin installs must always
  subscribe with their own plugin id.
