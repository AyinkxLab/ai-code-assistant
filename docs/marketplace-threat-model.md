# Marketplace threat model

This document is the focused threat model for the plugin marketplace and remote
install path. It complements the controls inventory in [Security model](security.md)
and the end-to-end trust walkthrough in [Plugins](plugins.md#marketplace-trust-model-end-to-end).

## Scope

In scope:

- the operator-controlled catalog (local file or `PLUGIN_MARKETPLACE_URL`);
- catalog entry validation, publisher signatures, and integrity metadata;
- owner-only catalog installation, dependency resolution, update/rollback, and
  the plugin lockfile;
- per-workspace capability grants and dispatch-time enforcement;
- plugin outbound network access through `app/services/plugin_network.py`.

Out of scope is listed under [Out of scope](#out-of-scope). The marketplace is a
distribution surface, not a privilege boundary: it never grants capabilities,
downloads artifacts, or executes plugin code.

## Trust boundaries

| Boundary | Untrusted input | Trusted side |
| -------- | --------------- | ------------ |
| Catalog source | Remote JSON fetched from a host on `PLUGIN_NETWORK_ALLOWLIST` | Operator-configured local catalog file or HTTPS URL |
| Catalog entry | Plugin id/version, source URL, `integrity`, embedded manifest, detached signature | Validated `CatalogEntry` after `app/services/plugin_catalog.py` |
| Publisher | Manifest bytes and Ed25519 signature | `PLUGIN_TRUSTED_KEYS` and `PLUGIN_TRUST_POLICY` |
| Install caller | `plugin_id` and optional `version` | Workspace owner holding `manage_plugins` |
| Plugin runtime | Handler code and event payloads | Capability grants, dispatch checks, timeouts, error isolation |
| Outbound request | Destination URL supplied by plugin/operator | `PluginNetworkPolicy` allowlist and SSRF guard |

## Threats and controls

### Supply chain

| Threat | Control | Evidence |
| ------ | ------- | -------- |
| Attacker replaces the catalog document | Local catalog is operator-owned; remote catalog must be HTTPS, on `PLUGIN_NETWORK_ALLOWLIST`, and is fetched through the bounded transport with redirects refused. Catalog parsing rejects the whole document on any invalid entry. | `app/services/plugin_catalog.py` (`load_remote_catalog`, `parse_catalog`); `app/services/plugin_network.py` (`guarded_request`) |
| Catalog entry points at an unexpected package | `id` must match the embedded manifest id and `version` must match the manifest version. `source` accepts only HTTPS URLs (or opaque local identifiers) and is length-bounded. | `parse_catalog_entry`, `_parse_manifest`, `_parse_source` |
| Artifact substitution after publication | Each entry carries a sha256 `integrity` value; it is normalized, validated, recorded on install, and pinned in the lockfile. The artifact is not downloaded by this release, so the digest is not yet re-verified against bytes. | `parse_catalog_entry`; `app/services/plugin_lockfile.py`; see [Residual risk](#residual-risk) |
| Dependency confusion or cycles | Install resolves the dependency graph against the same catalog and rejects cycles, missing entries, and disabled dependencies before any write. | `app/services/plugin_deps.py`; `validate_catalog_install` |
| A failed update leaves a half-installed plugin | Update/rollback run atomically; a failure preserves the previous installation and lock entry. | `app/services/plugin_ops.py`; `tests/test_plugin_catalog_install.py` |
| Untrusted discovery content becomes executable | Discovery and marketplace APIs return advertised metadata only. Catalog content is never executed and never grants capabilities. | `app/plugins/routes.py`; `docs/plugins.md` |
| Silent install-time code execution | Install validates and persists metadata only: no download, no import, no handler registration, no implicit grants. | `api_install_plugin_from_catalog`; `tests/test_plugin_catalog_install.py` |

### SSRF and outbound network

| Threat | Control | Evidence |
| ------ | ------- | -------- |
| Plugin reaches loopback, private, link-local, reserved, multicast, or unspecified addresses | `PluginNetworkPolicy.check_url` rejects private IP literals and obviously private hostnames; DNS resolution is checked against private ranges. `PLUGIN_NETWORK_ALLOW_PRIVATE` defaults to false. | `app/services/plugin_network.py`; `tests/test_plugin_network.py` |
| Plugin reaches any host | Every request must match `PLUGIN_NETWORK_ALLOWLIST` (exact, `*.suffix`, or explicit `*`). An empty allowlist denies every request. | `parse_allowlist`, `PluginNetworkPolicy.is_host_allowed` |
| Plaintext or credential-bearing URLs | HTTPS-only by default; URLs with embedded username/password are refused. Remote catalog URLs are HTTPS-only even if the broader policy disables HTTPS enforcement. | `check_url`; `load_remote_catalog` |
| Redirect pivots to an unguarded host | Redirects are never followed; any 3xx response is refused before the body is used. | `guarded_request`; `tests/test_plugin_network.py` |
| Resource exhaustion through a large or slow response | Request timeout, declared `Content-Length` cap, and actual body-size cap are enforced. | `guarded_request`; `PLUGIN_NETWORK_TIMEOUT` / `PLUGIN_NETWORK_MAX_BYTES` |
| DNS rebinding or a hostile resolver | Literal-IP, scheme, base-URL, and allowlist checks still apply; public resolution is best-effort by default and can fail closed with `PLUGIN_NETWORK_STRICT_DNS`. | `_assert_public_resolution`; see [Residual risk](#residual-risk) |
| SSRF through Stellar/Horizon or RPC endpoints | Endpoints come from configuration only; private/loopback literals rejected, redirects refused, base-URL restriction, timeout and size caps. | `app/services/stellar.py`; `app/services/soroban_rpc.py`; `docs/security.md` |

### Signature and integrity

| Threat | Control | Evidence |
| ------ | ------- | -------- |
| Forged or tampered manifest | Ed25519 signature verification over the canonical manifest body (compact, key-sorted JSON excluding `signature`, `trust_state`, and `trust_publisher`). Any invalid or untrusted signature is refused under every policy. | `app/services/plugin_signatures.py` (`canonical_manifest_body`, `enforce_publisher_trust`) |
| Unsigned entry when signatures are required | `PLUGIN_TRUST_POLICY=required` refuses missing signatures; `if-present` still refuses forged, malformed, or unknown-publisher signatures. | `enforce_publisher_trust`; `tests/test_plugin_signatures.py` |
| Attacker supplies their own publisher key | Only keys in operator-managed `PLUGIN_TRUSTED_KEYS` are trusted. Keys are validated at startup; API/CLI list publisher ids and policy but never key bytes or fingerprints. | `app/services/plugin_trust.py`; `tests/test_plugin_trust.py` |
| Detached signature authenticates different bytes than the manifest | Detached signatures are verified against the same canonical manifest body used for embedded signatures, so the two forms cannot authenticate different content. | `canonical_manifest_body`; `tests/test_plugin_signatures.py` |
| Client bypasses the catalog by uploading a manifest or key | `install-from-catalog` accepts only `plugin_id` and optional `version`; the entry, manifest, URL, integrity value, and signature all come from the operator catalog. | `api_install_plugin_from_catalog`; `tests/test_plugin_catalog_install.py` |
| Artifact integrity is assumed but not verified | The catalog digest is validated and recorded, but this release does not download artifacts; the digest must be re-verified when download is implemented. | [Residual risk](#residual-risk) |

### Capability isolation

| Threat | Control | Evidence |
| ------ | ------- | -------- |
| Plugin gains a capability implicitly on install/enable | Capabilities are explicit `CapabilityGrant` rows scoped to `(plugin_id, workspace_id, capability)`. Install, enable, update, and rollback never create grants. | `app/services/capabilities.py`; `tests/test_capabilities.py` |
| Unknown capability string is persisted | Grants validate against the `Capability` enum and fail closed on unknown values. | `validate_plugin_capability`; `tests/test_capabilities.py` |
| Cross-workspace access | Grants are workspace-scoped with a uniqueness constraint; dispatch requires the plugin to be installed and enabled in the event's workspace. | `app/services/events.py`; `tests/test_event_authorization.py` |
| Confused deputy through event context | Dispatch verifies event type support, plugin enabled state, workspace membership, and the capability mapped by `EVENT_CAPABILITY_MAP` before a handler runs. Missing context fails closed. | `app/services/events.py` |
| Unauthorized caller installs or configures a plugin | Management endpoints require `manage_plugins`; non-members receive 404 and non-owners 403. Authorization comes from workspace membership, never client-supplied ownership. | `app/plugins/routes.py`; `tests/test_plugins_api.py` |
| Handler failure crashes a request | Dispatch and lifecycle hooks are isolated; failures are recorded as bounded error reports and never propagate as request failures. | `app/services/plugin_errors.py`; `tests/test_event_wiring.py` |
| Workspace config/secrets leak through list/inspect surfaces | Plugin config is owner-only and omitted from list/inspect responses; size, key count, and nesting depth are capped. | `app/services/plugin_config.py`; `tests/test_plugin_config.py` |
| Capability changes are invisible | Grant/revoke/enable/disable and dispatch denials are appended to the owner-visible audit trail without payloads. | `app/services/plugin_audit.py`; `tests/test_plugin_audit.py` |

## Residual risk

- **DNS resolution is best-effort by default.** A hostname that resolves to a
  private address after the check, or a hostile resolver, is not fully
  preventable. Operators should keep `PLUGIN_NETWORK_ALLOWLIST` narrow and may
  enable `PLUGIN_NETWORK_STRICT_DNS`.
- **Artifact bytes are not verified yet.** Catalog `integrity` is validated as a
  sha256 digest and pinned, but artifacts are not downloaded. A future download
  step must re-verify the digest before use.
- **Signatures cover the manifest, not artifact bytes.** A trusted publisher
  must publish the same manifest that matches the artifact; artifact hashing is
  the remaining link.
- **A compromised trusted publisher key is trusted.** Key rotation, revocation,
  and compromise detection are operator procedures, not automated.
- **Global events lack workspace context.** `github.connected` and
  `github.disconnected` are delivered to enabled subscribers without a
  per-workspace grant check; plugin installs must not rely on them for
  workspace-scoped authorization.
- **Internal subscribers without a `plugin_id` bypass plugin capability
  checks.** They are trusted host code; plugin code must always subscribe with
  its own id.
- **In-process handlers are not a sandbox.** CPU, memory, and Python-level
  isolation are not enforced beyond timeouts, bounded errors, and capability
  checks.
- **Local catalog integrity is an operator responsibility.** File permissions
  and the trustworthiness of the local filesystem are outside the application.
- **Stellar/Horizon endpoint confidentiality** depends on operator
  configuration; the guard prevents SSRF but does not attest to the endpoint's
  honesty.

## Out of scope

- Downloading, unpacking, or executing plugin artifacts (not implemented in this
  release).
- OS/container-level sandboxing of plugin code.
- Protection against a compromised host, database, session, or operator account.
- Availability attacks that exhaust process resources beyond the configured
  request timeout and response-size caps.
- Plugin sources outside the operator catalog, including arbitrary client URLs.
- Confidentiality of secrets stored in workspace plugin configuration (owner
  responsibility; the application caps size but does not encrypt at rest).
- Security of third-party package registries used to build the application.

## Review triggers

Update this threat model when any of the following changes:

- catalog format, trust policy, signature scheme, or trusted-key handling;
- install, update, rollback, or lockfile behavior;
- the plugin network guard, allowlist semantics, or DNS policy;
- capability enum, role mapping, event capability map, or dispatch checks;
- artifact download/execution is implemented;
- a new plugin execution surface is wired up.

## Verification

The controls above are exercised by the plugin test suite, including
`tests/test_plugin_network.py`, `tests/test_plugin_signatures.py`,
`tests/test_plugin_trust.py`, `tests/test_plugin_catalog_install.py`,
`tests/test_plugin_deps.py`, `tests/test_capabilities.py`,
`tests/test_event_authorization.py`, `tests/test_plugin_audit.py`, and
`tests/test_plugin_config.py`. Documentation links are checked by
`tests/test_docs_links.py`.
