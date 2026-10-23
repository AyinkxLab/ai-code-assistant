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
- **Outbound is opt-in.** Plugin network access is denied unless an operator
  explicitly allowlists the target host; private and internal ranges are never
  reachable by default.
- **Read-only by construction.** The Stellar/Horizon/RPC surface never signs,
  simulates, or submits transactions, and never stores or handles keys.

## Threat review

| Threat                          | Control                                                                                          | Status |
| ------------------------------- | ------------------------------------------------------------------------------------------------ | ------ |
| Plugin privilege escalation     | Manifests declare capabilities; runtime grants require an explicit `CapabilityGrant` per workspace (`CapabilityStore.grant`). Roles are mapped to capabilities in `ROLE_CAPABILITY_MAPPING`; `validate_plugin_capability` fails closed. | Implemented |
| Unauthorized plugin capabilities| Capabilities are validated against the `Capability` enum before granting; unknown strings are rejected. | Implemented |
| Workspace isolation             | Capability grants are scoped `(plugin_id, workspace_id, capability)` with a uniqueness constraint. | Implemented |
| Project isolation               | The Stellar AI analysis reuses `assert_content_access` (owner-only, fails closed). Project files are only ever read for projects the caller may access. | Implemented |
| GitHub analysis authorization   | Stellar-aware PR/issue analysis is detection-driven; the bounded repo slice is fetched through the user's own GitHub token (GitHub's permission model decides access). No manual `stellar=true` flag exists and no additional access is granted. | Implemented |
| Prompt-injection resistance (GitHub analysis) | The Stellar PR/issue guidance frames PR/issue text, commit messages, and repository files as untrusted data, not instructions; detection never follows content from untrusted sources. | Implemented |
| Stellar data access             | `StellarService` is read-only; it never signs, sends, or funds. | Implemented |
| Secret exposure                 | `.env`/key files are skipped at project import; the Stellar prompt explicitly flags hard-coded credentials; service config uses env vars only; no secrets are stored or logged. | Implemented |
| Endpoint manipulation           | Endpoint URLs come from `current_app.config` only. | Implemented |
| SSRF                           | `validate_endpoint_url`: https-only for public networks, loopback-only for custom networks; requests are restricted to the configured Horizon base URL and bounded by timeout + response-size cap. | Implemented |
| Path traversal                  | Project file access already rejects traversal at import and in file/tree APIs; Stellar detection only reads `path`/`content` of already-indexed files. | Existing + verified |
| Malicious plugin metadata       | Manifest validation rejects bad ids, versions, entry points, unknown capabilities, and empty capability lists. | Implemented |
| Plugin outbound requests (SSRF) | `app.services.plugin_network` is the only sanctioned outbound path: https-only, denies private/loopback/link-local/reserved IP literals and obviously-private hostnames, denies hostnames that resolve to a private address (best effort), and only reaches hosts on `PLUGIN_NETWORK_ALLOWLIST`; an empty allowlist denies everything (fail closed). Requests never follow redirects and are timeout/size-bounded. | Implemented |
| Plugin management authorization | The `plugins` API is workspace-scoped: members may view, only the owner (`manage_plugins`) may install/enable/disable/grant. Non-members get 404, non-owners get 403; the server derives authorization from trusted workspace membership, never from client-supplied ownership. | Implemented |
| Plugin identity binding         | Installation binds the plugin to the id declared in the validated manifest; a conflicting entry point for an existing id is rejected (409). Install never loads or executes code and never auto-grants capabilities; grants are restricted to manifest-declared capabilities. | Implemented |
| Event authorization             | Before a plugin handler runs, the dispatcher verifies the event type is supported, the plugin exists and is enabled, and for workspace-scoped events the plugin is installed and enabled in the event's workspace, the emitting user is authorized for the workspace, and the plugin holds the capability required by `EVENT_CAPABILITY_MAP` via an explicit `CapabilityGrant` for that workspace. Unknown/missing/disabled/unauthorized cases are denied (fail closed); denials are recorded, never delivered. | Implemented |
| RPC redirect following           | `StellarService` and `SorobanRpcClient` set `allow_redirects=False`; any 3xx response is treated as an error (fail closed), so a redirect can never move a validated request to an unvalidated host. | Implemented |
| RPC host/IP validation           | Public-network endpoints must be `https`, must not be a private/link-local/loopback/reserved IP literal or an obviously-private hostname, and are DNS-verified (best-effort) to resolve to a globally routable address before each request. Custom networks remain loopback-only. | Implemented |
| RPC out-of-base requests         | `_check_url` restricts requests to the configured Horizon/RPC base URL; anything else is refused. | Implemented |
| RPC bounds                       | Timeout (`STELLAR_REQUEST_TIMEOUT`), response-body cap (`STELLAR_MAX_RESPONSE_BYTES`), max ledger keys per call (`STELLAR_RPC_MAX_KEYS`), bounded results, and raw-XDR length caps. | Implemented |
| RPC malformed responses          | Non-JSON-RPC payloads, id mismatches, HTTP errors, and RPC error payloads map to typed `SorobanRpc*` errors; nothing is silently ignored. | Implemented |
| Stellar read-only enforcement    | `SorobanRpcClient` implements only read-only methods; `sendTransaction`/`simulateTransaction` are absent; the CLI and web APIs only wrap read-only operations. | Implemented |
| Stellar address validation       | Structural G-address checks plus full strkey checksum validation (SEP-23 CRC16-XMODEM) for account/contract inspection; the strkey alphabet correctly includes `I`/`O` (only `0`, `1`, `8`, `9` are excluded). | Implemented |
| Network selection                | Users can select only the fixed supported networks (testnet/mainnet/futurenet/local); mainnet is never an implicit default and raw URLs are never accepted. Stored selection only routes read-only requests through `STELLAR_NETWORK`-derived config. | Implemented |
| Plugin capability audit         | Every grant/revoke/enable/disable and every dispatch denial is appended to the owner-visible `ActivityEvent` audit trail with plugin/capability/action/outcome (never payloads). | Implemented |
| Structured error reports        | Plugin handler/lifecycle failures are recorded as bounded `PluginErrorReport` rows (plugin id, operation, exception type, truncated message). No stack traces, payloads, or secrets by default; workspace reports are owner-scoped; recording never affects failure isolation. | Implemented |
| Config secrecy                  | Per-workspace plugin config is validated and stored on `PluginInstallation.config`, is **omitted** from list/inspect surfaces, and is only read/updated by the workspace owner. | Implemented |
| Compatibility enforcement       | A plugin’s optional PEP 440 `compatibility` is validated at manifest parse and enforced against the running app version at install; invalid/incompatible plugins are refused. | Implemented |
| Lifecycle hooks + uninstall     | Optional hooks run isolated/non-fatal on registry enable/disable/uninstall; the workspace uninstall endpoint revokes every capability grant for that workspace before removing the installation. | Implemented |
| Plugin events                   | `plugin.enabled`/`plugin.disabled`/`plugin.uninstalled` are workspace-scoped events mapped to `WORKSPACE_READ` (dispatch-time enforcement applies). | Implemented |
| Plugin CLI                      | `flask plugins …` acts as the operator on persisted rows; installs are local-path only (URLs refused) and never grant capabilities or load code. | Implemented |
| Publisher signatures (detached) | Ed25519 signatures are verified over the canonical manifest body (compact sorted JSON excluding `signature`/trust fields), whether embedded in the manifest or detached in a `.sig`/catalog field. Forged, malformed, and unknown-publisher signatures are refused under **every** policy; unsigned entries are refused when `PLUGIN_TRUST_POLICY=required`. Verification never echoes key material. | Implemented |
| Trusted publisher key management | `PLUGIN_TRUSTED_KEYS` must be a JSON object of canonical publisher ids to valid 32-byte Ed25519 keys (base64 raw or PEM); malformed keys/policies abort startup with a clear message. The API (`GET /plugins/api/trusted-publishers`) and CLI (`flask plugins publishers`) expose publisher ids and the policy only — never key bytes or fingerprints. | Implemented |
| Catalog-pinned remote install   | `POST .../plugins/install-from-catalog` is owner-only and resolves `(plugin_id, version)` server-side against `PLUGIN_CATALOG_FILE`; clients cannot supply a manifest, artifact URL, or key. Entries are validated (canonical id/version, https source, sha256 integrity, manifest id/version match) and the trust policy is enforced before any write. It registers metadata only: no download, no code execution, no implicit capability grants; refusals are audited with no artifact contents. | Implemented |
| Remote catalog fetch            | Optional `PLUGIN_MARKETPLACE_URL` is fetched only when no local catalog file is configured. https and `PLUGIN_NETWORK_ALLOWLIST` are checked before any request; redirects, private DNS resolution, oversized bodies, and non-allowlisted hosts are refused. The fetch uses the bounded plugin transport (timeout, size cap, no redirects). | Implemented |
| Budget authorization           | User budgets are readable and writable only by their owner. Workspace budgets are readable and writable only by the workspace owner or a site administrator. Missing and inaccessible budgets return the same `404` payload, so ids cannot be probed. Dashboards derive spend from the usage ledger, never from budget input. | Implemented |
| Plugin lock entries             | `PluginLockEntry` rows are scoped to `(workspace_id, owner_id)` with a unique `(workspace_id, plugin_id)` constraint, store only resolved metadata (id, version, source, sha256 integrity, dependencies), and imply no capability grant. Canonical serialization omits volatile timestamps for reproducible diffs. | Implemented |
| Stellar security findings       | `stellar_security` findings are evidence-labelled rows owned by a single project; reads are owner-scoped (404 for non-owners, no existence oracle). | Implemented |
| XDR decoding                    | Transaction/envelope + contract-data decoding is bounded and read-only; decoded values are never guessed, malformed/unsupported XDR is explicit, and no signing/submission/keys exist. | Implemented |

## Testing

Security-relevant coverage includes:

- `tests/test_stellar_service.py` — endpoint validation, SSRF (out-of-base and
  non-loopback rejection), size caps, error taxonomy.
- `tests/test_stellar_security.py` — adversarial endpoint/host validation
  (private/link-local/reserved IPs, obviously-private hostnames, redirect
  refusal, request-time host resolution, strict-validation toggle).
- `tests/test_soroban_rpc.py` — the RPC client: method behavior, malformed
  responses, redirect refusal, size caps, private-host resolution rejection,
  and invalid-parameter fail-closed cases.
- `tests/test_stellar_xdr.py` — strkey checksum validation and LedgerKey
  encoders verified against authoritative Stellar fixtures.

### Stellar SSRF adversarial matrix

`tests/test_stellar_security.py` proves the guards fail closed across:
private/loopback/link-local/reserved IP literals (IPv4 + IPv6),
obviously-private hostnames, redirect refusal for both clients, request-time
host resolution via an injected resolver, the strict-validation toggle,
response-size caps, 404/malformed-JSON mapping, and **timeout aborts** against
a loopback HTTP server that never responds (the clients raise the typed
`NetworkError` / `SorobanRpcUnavailableError` instead of hanging).

Host forms are normalized before every check: the scheme and host are
lowercased and a single trailing dot is stripped, so
`HTTPS://HORIZON.STELLAR.ORG.` is equivalent to `https://horizon.stellar.org`,
while `https://127.0.0.1./` is rejected. Percent-encoded or otherwise unsafe
hosts are rejected outright, so an encoded private literal (e.g.
`https://%31%32%37.0.0.1/`) can never bypass the literal checks.

**DNS bypass limitation:** only guard logic is testable offline. A public
hostname that later resolves to a private address is caught at request time by
the injectable `host_resolver`; real DNS behavior cannot be proven without
network access, and public RPC/Horizon endpoints remain operator-controlled
configuration (see "Known limitations / planned hardening").
- `tests/test_stellar_inspection.py` — account/contract/ledger inspection and
  honest "unavailable" handling.
- `tests/test_stellar_analysis.py` — non-owner and unauthenticated users fail
  closed; non-Stellar projects are reported honestly.
- `tests/test_stellar_analysis_github.py` — detection-driven PR/issue analysis:
  Stellar context only when detected, non-Stellar and plain-Rust repos stay
  generic, detection failure never crashes, confidence is respected, context is
  bounded, and the GitHub routes preserve user-token authorization.
- `tests/test_plugins_manifest.py` — malicious/invalid manifests rejected.
- `tests/test_plugin_network.py` — the plugin outbound guard: allowlist
  (exact/`*`/wildcard), non-https and malformed URLs, loopback/private/
  link-local/reserved ranges, obviously-private hostnames, private DNS
  resolution, the `allow_private`/`strict_dns` toggles, and the bounded
  `guarded_request` helper (guard runs before any socket is opened).
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
- `tests/test_stellar_security_findings.py` — findings parsing/normalization,
  persistence, owner read access, and non-Stellar gating.
- `tests/test_stellar_network_switcher.py` — validated network selection,
  mainnet-default protection, persistence, and SSRF-safe routing.
- `tests/test_stellar_mock_network.py` — end-to-end reads against the offline
  mock network.
- `tests/test_stellar_xdr_transaction.py` — bounded transaction-envelope XDR
  decoding (supported ops, unsupported/malformed/truncated cases).
- `tests/test_plugin_integration.py` — end-to-end plugin flow and
  failure-isolation with real dispatch.
- `tests/test_plugin_audit.py` — capability/state audit trail, owner-only
  reads, no sensitive-data leakage.
- `tests/test_plugin_error_reports.py` — bounded error recording, dispatch/hook
  failures, owner-scoped endpoint, operator CLI.
- `tests/test_plugin_config.py` — owner-scoped config read/update and
  validation (unknown keys/types rejected).
- `tests/test_plugin_compat.py` — PEP 440 compatibility parsing/enforcement.
- `tests/test_plugin_lifecycle.py` — lifecycle hooks and plugin events.
- `tests/test_plugins_cli.py` — CLI commands, `--json`, exit codes, URL
  refusal, and no implicit grants.
- `tests/test_plugin_trust.py` — trusted publisher key decoding/validation,
  startup rejection of malformed keys or policies, and secret-free listing.
- `tests/test_plugin_signatures.py` — canonical signing payload, detached
  signature verification, forged/unsigned refusal, and embedded-signature
  compatibility.
- `tests/test_plugin_catalog_install.py` — catalog validation plus the
  owner-only remote-install endpoint (trust policy, failure codes, audit of
  refusals, no code execution, no implicit grants).
- `tests/test_plugin_lock_entries.py` — lock-entry scoping, uniqueness, and
  canonical lockfile serialization.

## Marketplace trust model

The marketplace is a distribution surface, not a privilege boundary. Trust is
established in layers, and every layer fails closed. The focused threat model,
including residual risk and out-of-scope items, is documented in
[Marketplace threat model](marketplace-threat-model.md).

1. **Source.** The catalog is operator-controlled: a local JSON document
   (`PLUGIN_CATALOG_FILE`, or `PLUGIN_MARKETPLACE_CATALOG` for the read-only
   discovery API) or a remote document fetched through
   `app/services/plugin_network.py`. Remote fetches must use HTTPS, the host
   must be on `PLUGIN_NETWORK_ALLOWLIST`, and the request is timeout/size
   bounded with redirects refused.
2. **Entry validation.** Each entry is parsed by
   `app/services/plugin_catalog.py`: canonical plugin id/version, HTTPS
   artifact source, sha256 `integrity`, an embedded manifest whose id/version
   match the entry, and an optional detached Ed25519 signature. Discovery
   entries additionally validate the manifest against
   `plugins/plugin.schema.json`. Malformed entries reject the catalog.
3. **Publisher identity.** `app/services/plugin_signatures.py` verifies the
   signature over the canonical manifest body. Forged, malformed, and
   unknown-publisher signatures are refused under every policy; with
   `PLUGIN_TRUST_POLICY=required`, unsigned entries are refused too.
   `PLUGIN_TRUSTED_KEYS` is validated at startup and key material is never
   exposed by the API or CLI.
4. **Install.** Catalog installs are owner-only, resolve `(id, version)`
   server-side, and validate the dependency graph
   (`app/services/plugin_deps.py`) before writing anything. Cycles and missing
   catalog dependencies are rejected. Install registers metadata only: it does
   not download or execute the artifact and does not create capability grants.
5. **Update / rollback.** Updates resolve a catalog target and run atomically;
   a failed update leaves the previous installation intact. Rollback restores
   the lockfile/catalog target. Both require `manage_plugins`, and every
   mutation is audited.
6. **Lockfile.** `PluginLockEntry` pins id, version, source, sha256 integrity,
   and declared dependencies per workspace. Serialization is canonical and
   omits timestamps, so diffs are reproducible. The lockfile never contains
   secrets and never implies a capability grant.
7. **Read-only discovery.** `GET /plugins/api/marketplace` returns advertised
   metadata only. Catalog content is untrusted input: it is never executed and
   never used to grant a capability.

Implementation and tests are listed in
[the plugin guide](plugins.md#marketplace-trust-model-end-to-end). The
threat-review rows above cover the concrete controls; this section is the
end-to-end view for reviewers and operators.

## Known limitations / planned hardening

- Custom-network endpoints are restricted to loopback; a production operator
  using a remote custom node needs to extend the allow-list policy.
- Installing/registering a plugin (API or CLI) **never loads or executes**
  plugin code; executing real plugin modules at runtime (auto-loading entry
  points, subscribing their handlers) is intentionally not wired to any
  install flow yet — plugins are subscribed in-process by code, and a future
  runtime must keep capability enforcement and review in front of execution.
- Catalog installs register **metadata only**: the catalog's `integrity` hash
  is validated as a sha256 digest and returned/recorded, but the artifact is
  not downloaded yet. A future download step (bounded transport + hash
  verification) must re-verify the digest before any artifact is used.
- Plugins have **no outbound network capability wired up yet**. The guard in
  `app/services/plugin_network.py` (and its allowlist policy) is the required
  path for any future plugin-triggered request; plugin code must call
  `guarded_request` rather than `requests` directly. The DNS-level private-IP
  check is best-effort (a resolution failure is tolerated unless
  `PLUGIN_NETWORK_STRICT_DNS` is set), so operators should keep the allowlist
  narrow.
- Global events (`github.connected`, `github.disconnected`) carry no workspace
  context and are delivered to any enabled plugin that subscribes; capability
  grants are workspace-scoped, so a per-workspace grant check does not apply to
  them.
- Subscribers registered without a `plugin_id` are treated as trusted internal
  handlers and bypass plugin capability checks; plugin installs must always
  subscribe with their own plugin id.
- The DNS-level public-host validation is best-effort (a DNS failure is
  tolerated because the scheme/literal/base-URL guards still apply). It guards
  against misconfiguration, not a hostile resolver; public RPC endpoints remain
  operator-controlled configuration.
- The `/stellar` read-only endpoints are login-required and lightly rate
  limited but not workspace-scoped: they query public network data bound to the
  configured network (equivalent to a block explorer).

## Repository supply-chain protection

The application code inside this repository protects its users, but the
repository itself can also be used as an attack vector: a commit can hide an
executable payload inside a file that looks harmless, or a workflow can hand
its secrets to code from a pull request. This repository has been attacked
exactly that way — a commit once shipped an obfuscated JavaScript payload
disguised as a web font together with a `.vscode/tasks.json` that ran it on
every folder open.

Two layers of defence are now in place against that class of attack:

### 1. The asset and supply-chain guard (`scripts/check_assets.py`)

Every CI run executes this guard (the **Asset & VS Code guard** job in
`ci.yml`). It scans the tree for:

| Area                          | What it rejects                                                                          |
| ----------------------------- | ---------------------------------------------------------------------------------------- |
| Assets                        | Font-extension files that are not real fonts, or that contain script markers.             |
| Editor tasks                  | `.vscode` files that run on `folderOpen`, enable `task.allowAutomaticTasks`, or shell out. |
| Workflow triggers             | `pull_request_target` and `workflow_run` — both run with the base repository's privileges on fork pull requests. |
| Workflow permissions          | Every workflow must declare a top-level `permissions:` block; write scopes are limited to an allowlist (`contents: read` everywhere, plus `security-events: write` for CodeQL only). |
| Action pinning                | Third-party actions must be pinned to a full 40-character commit SHA, not a mutable tag.   |
| Workflow secrets             | `pull_request`-triggered workflows must not read `secrets.*`.                              |
| Checkout credentials          | Every `actions/checkout` step must set `persist-credentials: false` so the CI token is never handed to pull-request code. |
| Python requirements           | Exact `==` pins only; no `--extra-index-url`, no direct-URL or VCS requirements.           |
| npm manifests                 | No install lifecycle scripts; the lockfile must match `package.json` and resolve only from `registry.npmjs.org` with an integrity hash. |
| Dockerfiles / install scripts | No `curl … \| sh`, no `--trusted-host`/`-k` insecure transport, no `ADD <url>`, no unpinned installs. |
| Ignore rules                  | `.gitignore` must not ignore itself (that is how rule changes get hidden).                |
| The guard itself              | The guard carries a tripwire: if its checks are removed, CI fails; the guard test must also be present. |

The guard's behaviour is tested by `tests/test_asset_guard.py`, which builds
fake repository trees in temporary directories and asserts both that each
attack shape above is caught and that clean trees produce no false positives.

### 2. The CI hardening (`/.github/workflows/`)

The workflows apply the principle of least privilege to CI itself:

- Every workflow and job declares `permissions: contents: read` at minimum.
- All actions are pinned to verified commit SHAs.
- All checkouts run with `persist-credentials: false`.
- Every job has a `timeout-minutes` ceiling and a `concurrency` group that
  cancels superseded runs, so a malicious commit cannot pin a runner.
- CodeQL (`codeql.yml`) analyses `python`, `javascript`, and `actions` with the
  `security-extended` query suite on every push to `main` and every pull
  request, and weekly on a schedule.
- Dependency Review (`dependency-review.yml`) fails a pull request that
  introduces a dependency with a known high or critical advisory. It requires
  the repository's dependency graph to be enabled (`Settings` → `Code
  security` → `Advanced Security` → `Dependency graph`); without it the job
  fails rather than silently passing.
- Dependency Audit (`dependency-audit.yml`) runs `pip-audit` on both
  requirements files and `npm audit` against the lockfile on every push,
  pull request, and weekly schedule.
- Dependabot (`.github/dependabot.yml`) opens weekly version-bump pull
  requests for pip, npm, GitHub Actions, and Docker.

### Code-owner review

`.github/CODEOWNERS` requests a review from the repository owner for every
path where a change can silently widen this trust boundary (workflows, the
guard, dependency manifests, build files, `.vscode`, fonts, ignore rules).
Combined with a branch-protection rule that *requires* code-owner review (a
GitHub web setting the owner must enable), this makes those files effectively
maintainer-gated.

### What contributors must not do

Contributors must not weaken or bypass the guard, add lifecycle scripts to
`package.json`, add unpinned dependencies, or add auto-run editor tasks.
See the "Supply-chain rules for contributors" section of
[CONTRIBUTING.md](../CONTRIBUTING.md) for the contributor-facing version of
these rules, and [SECURITY.md](../SECURITY.md) for how to report an attempt to
introduce any of them.
