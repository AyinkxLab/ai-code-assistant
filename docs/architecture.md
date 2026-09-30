
# Architecture

This document describes the high-level architecture of the AI Code Assistant,
with a focus on how the Stellar/Soroban developer tooling fits in.

## Overview

The AI Code Assistant is a developer-focused AI code intelligence platform. It
is a Flask application (application-factory pattern) with PostgreSQL
persistence, a vanilla-JS/CSS frontend, and a service layer that keeps the
application logic separate from the web layer.

```
Browser (vanilla JS)
   │  JSON / SSE
   ▼
Flask blueprints            app/<blueprint>/
   │                        auth, chat, collaboration, github, main, plugins,
   │                        prompts, reviews, stellar, tools, workspaces
   ▼
Service layer               app/services/
   │                        analysis, github, importing, llm, permissions,
   │                        reviews, search, stellar, soroban_rpc,
   │                        stellar_inspection, stellar_detection, …
   ▼
Persistence                 SQLAlchemy models (app/models/) + Alembic (migrations/)
```

## Layering

### 1. Web layer (blueprints)

Blueprints own HTTP concerns: authentication (`@login_required`), JSON
serialization, CSRF, and template rendering. Authorization decisions are
delegated to the service layer (`app/services/permissions.py`); routes never
re-implement security. The `stellar` blueprint exposes read-only Stellar
developer APIs and the `/stellar` page.

### 2. Service layer

Services implement the real logic:

- **LLM** (`llm.py`) — provider-agnostic completions with an offline mock
  provider by default.
- **GitHub** (`github.py`) — OAuth, repository/commit/issue/PR data, typed
  errors, retries, bounded context.
- **Importing** (`importing.py`) — safe archive/GitHub import with
  path-traversal, size, and secret-file guards.
- **Exporting** (`exporting.py`) — streams a project snapshot zip (#107) built
  entirely in memory from stored rows (no filesystem); binary/oversized files
  become clearly marked `.PLACEHOLDER.txt` stubs and a JSON manifest documents
  what was included.
- **Workspaces / analysis** (`project_analysis.py`) — bounded context
  retrieval, project chat, and project analyses (including the Stellar-aware
  kinds).
- **Stellar** (`stellar.py`, `soroban_rpc.py`, `stellar_inspection.py`,
  `stellar_detection.py`, `stellar_xdr.py`, `stellar_xdr_decode.py`,
  `stellar_mock.py`, `stellar_findings.py`, `soroban_scaffold.py`) — see
  [Stellar architecture](#stellar-architecture).
- **Plugins** (`plugins.py`, `capabilities.py`, `events.py`, `plugin_audit.py`,
  `plugin_compat.py`, `plugin_config.py`, `plugin_errors.py`, `plugin_ops.py`)
  — manifest parsing/validation, explicit per-workspace capability grants,
  capability-checked event dispatch, audit + bounded error reporting,
  PEP 440 compatibility, per-workspace configuration, and the operator CLI
  logic. See [Plugin architecture](#plugin-architecture).

### 3. Persistence layer

SQLAlchemy models in `app/models/`, schema changes via Flask-Migrate/Alembic.
Model changes are avoided unless genuinely required.

## Stellar architecture

```
Stellar config (env)                STELLAR_NETWORK, STELLAR_HORIZON_URL,
   │                                STELLAR_RPC_URL, timeouts, caps
   ▼
resolve_network_config()            app/services/stellar.py
   ▼
+-------------------+     +----------------------+     +----------------------+
| StellarService    |     | SorobanRpcClient     |     | stellar_xdr          |
| (Horizon, parsed) |     | (Stellar RPC, read)  |     | (strkey + LedgerKey) |
+-------------------+     +----------------------+     +----------------------+
   │  accounts, txs,            │  health, ledgers, entries, events, …
   │  ledgers, assets           │  contract/code inspection
   ▼                            ▼
+--------------------------------------------------------------+
| stellar_inspection  →  inspect_account / inspect_contract /   |
|                       inspect_ledger_entry / network_status    |
+--------------------------------------------------------------+
   │
   ├──▶ app/stellar/  (page + read-only APIs)
   ├──▶ project explorer "Stellar" tab (per-project detection)
   ├──▶ flask stellar …  (CLI)
   └──▶ AI analysis (project_analysis.stellar / stellar_security)
```

The Stellar layer is **read-only** and **configuration-driven**:

- Endpoint URLs come only from environment configuration (validated presets or
  operator overrides), never from users or imported projects.
- Public networks require https and are checked to resolve to public
  addresses; custom networks are loopback-only.
- Redirects are refused, response bodies are size-capped, and every request
  has a timeout.
- Nothing signs, simulates, or submits transactions, and no secrets/keys are
  ever stored or handled.

### Stellar/Soroban project detection

`stellar_detection.py` classifies imported projects from file-level evidence
only (never network calls):

- `likely` — Soroban crate in `Cargo.toml`, or Rust contract attributes /
  `soroban_sdk::` imports.
- `possible` — Stellar SDK dependency, Stellar/Soroban config files,
  `.soroban` directories, `contracts/` layout, or Stellar/Soroban CLI tooling.
- `none` — otherwise (a plain Rust crate is never classified as Soroban).

`detect_stellar_network` additionally extracts a network hint
(testnet/mainnet/futurenet) from config passphrases and file names, never from
live data.

### Stellar-aware AI analysis

`project_analysis.py` adds `stellar` and `stellar_security` analysis kinds that:

- Reuse the Phase 7 content-access gate (owner-only, fails closed).
- Report honestly when a project is not Stellar (no fabricated claims).
- Ground every claim in the indexed files, mark `[CONFIRMED]` vs `[SUGGESTION]`,
  and never claim live data the RPC could not provide.

Supporting read-only modules:

- `stellar_xdr_decode.py` — bounded, fixture-pinned decoding of `LedgerKey`,
  `LedgerEntryData`, `SCVal`s, and transaction envelopes (V1/V0/fee-bump) with
  common operations; unsupported/malformed XDR is reported explicitly.
- `stellar_mock.py` — a deterministic offline Horizon + Stellar RPC server used
  by tests and local development (no external network).
- `stellar_findings.py` — defensive parsing + per-project persistence of
  `stellar_security` findings (owner-scoped reads).
- `soroban_scaffold.py` — deterministic Soroban contract scaffold generation
  (no cargo/network; importable into a workspace).

## Plugin architecture

Plugins are **workspace-scoped** at runtime but globally declared:

- A validated `manifest.json` (`plugins.py`) describes a plugin; the management
  API/CLI persist it as a `Plugin` row and per-workspace `PluginInstallation`
  rows (enabled state + `config`). Install/enable/disable/uninstall never load
  or execute plugin code and never grant capabilities implicitly.
- Capabilities are granted explicitly per workspace (`CapabilityStore` →
  `CapabilityGrant`) and restricted to manifest-declared capabilities.
- `events.py` dispatches supported events; before a plugin handler runs, it
  verifies the plugin is installed+enabled in the event's workspace, the
  emitting user is authorized, the event's project/workspace context is
  consistent, and the plugin holds the event's mapped capability
  (`EVENT_CAPABILITY_MAP`). Handler failures are isolated.
- Security-relevant actions append to the owner-visible audit trail
  (`plugin_audit.py` → `ActivityEvent`); handler/lifecycle failures are
  recorded as bounded `PluginErrorReport` rows (`plugin_errors.py`).
- Operator surfaces: the workspace management API (`app/plugins/routes.py`),
  the plugin UI page, and the `flask plugins …` CLI (`plugins_cli.py` +
  `plugin_ops.py`), which supports `--json`, distinct exit codes, and local-only
  installs. See [docs/plugins.md](plugins.md).

## Security model

See [docs/security.md](security.md) for the full threat review. Highlights:

- Fail-closed authorization (`assert_content_access`, workspace scoping).
- Untrusted repository content is treated as data, never instructions
  (prompt-injection resistance).
- SSRF-bounded outbound networking (scheme, host, base-URL, redirect,
  size, and DNS-level guards).
- Secrets are never stored or logged; secret files are skipped on import.
- The Stellar/RPC surface is read-only by construction.

## Testing

- `tests/` is a pytest suite running against an in-memory SQLite database.
- Network behavior is tested with deterministic fixtures and mocked transport
  (no real network access); the encoders are verified against authoritative
  Stellar fixtures.
- `ruff check .` and `black --check .` must stay green (CI enforces this).

## GitHub integration

The GitHub integration lets users connect a GitHub account, browse
repositories, and import repository content into a workspace. It spans three
layers: the OAuth/HTTP surface (`app/github/routes.py`), the API client and
OAuth logic (`app/services/github.py`), and encrypted token storage
(`app/services/crypto.py`).

### Setup

The setup steps mirror the README's GitHub section. In summary:

1. Create a GitHub OAuth App (Settings → Developer settings → OAuth Apps) and
   set the **Authorization callback URL** to
   `<APP_BASE_URL>/github/callback`.
2. Copy the **Client ID** and generate a **Client secret**.
3. Configure the environment (see below) and restart the app.
4. Sign in, open the GitHub page, and click **Connect GitHub** to complete the
   OAuth handshake.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `GITHUB_CLIENT_ID` | OAuth App client ID. |
| `GITHUB_CLIENT_SECRET` | OAuth App client secret. |
| `GITHUB_OAUTH_SCOPES` | Space-separated scopes to request (defaults to the minimum needed, e.g. `read:user repo`). |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | Key material used by `crypto.py` to encrypt stored access tokens. |
| `GITHUB_API_BASE_URL` | API base URL; overridable for GitHub Enterprise/testing. |
| `GITHUB_API_TIMEOUT` | Per-request timeout in seconds. |

If the OAuth variables are unset, the GitHub integration is disabled and the
GitHub page reports that it is not configured rather than failing opaquely.

### Encrypted-token storage design

Access tokens are never stored in plaintext:

- `app/services/crypto.py` provides authenticated symmetric encryption
  (encrypt/decrypt) keyed from `GITHUB_TOKEN_ENCRYPTION_KEY`.
- `app/services/github.py` encrypts the token at rest and only decrypts it
  in-memory, immediately before an outbound API call.
- Tokens are scoped to the owning user and are never returned to the browser,
  logged, or included in error messages.
- A revoked or undecryptable token is treated as "not connected" and the user
  is prompted to reconnect.

### API client behaviour (retries and rate limits)

`app/services/github.py` wraps outbound calls with:

- **Timeouts** on every request (`GITHUB_API_TIMEOUT`).
- **Bounded retries** with backoff for transient failures (5xx, connection
  errors, and secondary rate limits), capped so a request cannot loop forever.
- **Rate-limit handling** — primary rate limits (`403`/`429` with
  `X-RateLimit-Remaining: 0`) surface as a typed error carrying the reset time
  instead of being retried blindly.
- **Typed errors** so the web layer can map failures to clear HTTP responses
  without leaking internals.

### Troubleshooting

- **Missing scope** — the API returns `403` for an endpoint the granted scopes
  do not cover. Disconnect and reconnect GitHub, ensuring the required scopes
  (see `GITHUB_OAUTH_SCOPES`) are granted.
- **Rate limited** — the client reports the reset time from the rate-limit
  headers. Wait until the reset, or reduce request volume; secondary limits
  are retried with backoff automatically.
- **Token revoked / invalid** — GitHub returns `401`. The stored token is
  treated as invalid, the connection is marked disconnected, and the user must
  reconnect to obtain a fresh token.
- **Not configured** — `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET` are unset. Set
  them and restart the app.
- **Decryption failures** — `GITHUB_TOKEN_ENCRYPTION_KEY` changed or is
  missing. Restore the original key or have affected users reconnect.

### Security note on token handling

GitHub access tokens are treated as secrets: they are encrypted at rest via
`crypto.py`, decrypted only in-memory for the duration of an API call, scoped
to the owning user, and never logged, echoed to the client, or persisted in
plaintext. Repository content fetched with a token is treated as untrusted
data, never as instructions.
