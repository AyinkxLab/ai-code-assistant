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
Flask blueprints            app/<bpueprint>/
                            auth, chat, collaboration, github, main, plugins,
   │                        prompts, reviews, stellar, tools, workspaces
   ▼
Service layer               app/services/
                            analysis, github, importing, llm, permissions,
   │                        reviews, search, stellar, soroban_rpc,
   │                        stellar_inspection, stellar_detection, ․
   ▾
Persistence                 SQLAlchemy models (app/models/) + Alembic (migrations/)
```

## Layering

### 1. Web layer (blueprints)

Blueprints own HTTP concerns: authentication (@login_required), JSON
serialization, CSRF, and template rendering. Authorization decisions are
delegated to the service layer (app/services/permissions.py); routes never
re-implement security. The `stellar` blueprint exposes read-only Stellar
developer APIs and the `/stellar` page.

### 2. Service layer

Services implement the real logic:

- `/services/llm.py` — provider-agnostic completions with an offline mock
  provider by default.
- `/services/github.py` — OAuth, repository/commit/issue/PR data, typed
  errors, retries, bounded context.
- `/services/importing.py` — safe archive/GitHub import with
  path-traversal, size, and secret-file guards.
- `/services/exporting.py` — streams a project snapshot zip (#107) built
  entirely in memory from stored rows (no filesystem); binary/oversized files
  become clearly marked `.PLACEHOLDER.txt` stubs and a JSON manifest documents
  what was included.
- `/services/project_analysis.py` — bounded context
  retrieval, project chat, and project analyses (including the Stellar-aware
  kinds).
- `/services/stellar.py`, `soroban_rpc.py`, `stellar_inspection.py`,
  `stellar_detection.py`, `stellar_xdr.py`, `stellar_xdr_decode.py`,
  `stellar_mock.py`, `stellar_findings.py`, `soroban_scaffold.py` — see
  [Stellar architecture](#stellar-architecture).
- `/services/plugins.py`, `capabilities.py`, `events.py`, `plugin_audit.py`,
  `plugin_compat.py`, `plugin_config.py`, `plugin_errors.py`, `plugin_ops.py`
  — manifest parsing/validation, explicit per-workspace capability grants,
  capability-checked event dispatch, audit + bounded error reporting,
  PEP 440 compatibility, per-workspace configuration, and the operator CLI
  logic. See [Plugin architecture](cplugin-architecture)

### 3. Persistence layer

SQLAlchemy models in `app/models/`, schema changes via Flask-Migrate/Alembic.
Model changes are avoided unless genuinely required.

## Workspaces and project import

Workspaces are the top-level ownership boundary. A `Project` belongs to a
workspace, and every project scoped resource (files, chat, analyses,
plugin installations, Stellar findings) is reached through that workspace.
Authorization is fail-closed and workspace-scoped (`assert_content_access`):
cross-workspace reads/cross-project reads are refused, even for authenticated
users.

### Project import

Importing is handled by `app/services/importing.py` and exposed through the
workspaces blueprint. Two sources are supported:

- Project archives (zip/tar). Guards enforce path-traversal rejection,
  bounded archive size and per-file size, and secret-file skipping (e.g.
  `.env`, key material). Nothing is executed during import.
- GitHub repositories. The GitHub service resolves the repo through the
  configured OAuth token, fetches a bounded tree, and imports files through the
  same sanitizing pipeline as archive import.

After import, files are indexed for search and analysis. The import pipeline
is deterministic and idempotent at the project level: re-importing a repo
updates the stored file set without executing anything from the repo.

### Search

`SearchService` (`app/services/search.py`) queries the indexed files of a
project. Results are workspace-scoped and never cross project boundaries.

### Chat and analyses

Chat and analyses are served by `app/services/project_analysis.py` and the
`chat` blueprint. The context builder is bounded: it selects a finite,
deterministic set of indexed files and never follows instructions found inside
repository content. Analyses include the Stellar-aware kinds described below.

### Health dashboard

The health dashboard exposes a read-only view of the application's
dependencies and integrations (database, LLM provider, GitHub, Stellar
RPC) including latency and error status. It never returns secrets or credentials.

## Stellar architecture

```
Stellar config (env)                STELLAR_NETWORK, STELLAR_HORIZON_URL,
   │                                STELLAR_RPC_URL, timeouts, caps
   ▾
resolve_network_config()            app/services/stellar.py
   ▼
+------------------+     +--------------------+     +--------------------+
| StellarService    |     | SorobanRpcClient     |     | stellar_xdr          |
| (Horizon, parsed) |     | (Stellar RPC, read)  |     | (strkey + LedgerKey) |
+------------------+     +--------------------+     +--------------------+
      accounts, txs,            │  health, ledgers, entries, events, …
   │  ledgers, assets              contract/code inspection
   ▼                            ▼
+---------------------------------------------------------------+
| stellar_inspection  →  inspect_account / inspect_contract /       |
|                       inspect_ledger_entry / network_status    |
+---------------------------------------------------------------+
    
   ├──▶ app/stellar/  (page + read-only APIs)
   ├──▶ project explorer "Stellar" tab (per-project detection)
   ├──"[ flask stellar …  (CLI)
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

- `likely` — Soroban crate in `CargoToml`, or Rust contract attributes /
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
  the plugin UI page, and the `flask plugins ․` CLI (`plugins_cli.py` +
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

## Developer notes: bounded context and prompt-injection defenses

The codebase is organized as bounded contexts. Each context owns its own
models, services, and blueprints, and cross-context access goes through
explicit service boundaries:

- Workspaces own projects. A project never reaches into another workspace.
- Projects own files, chat, and analyses. The context builder is bounded by
  size and by the project's indexed file set.
- Stellar read modules are configuration-driven and read-only; they never accept
  endpoints from users or imported content.
- Plugins are workspace-scoped and capability-gated.

All untrusted repository content is treated as data, never as instructions.
The context builder and the LLM prompt templates enforce this by:

- Wrapping file content in explicit data delimiters and labeling it as repo
  content.
- Instructing the model to ignore any instructions found inside repo content.
- Never interpreting repo content as tool calls, commands, or configuration
  changes.
- Keeping the context bounded so a malicious file cannot dominate the
  prompt.

These defenses are exercised by the prompt-injection test suite and are
considered a security invariant: changes to the context builder or prompt
templates must keep them intact.

## Testing

- `tests/` is a pytest suite running against an in-memory SQLite database.
- Network behavior is tested with deterministic fixtures and mocked transport
  (no real network access); the encoders are verified against authoritative
  Stellar fixtures.
- `ruff check .` and `black --check .` must stay green (CI enforces this).
