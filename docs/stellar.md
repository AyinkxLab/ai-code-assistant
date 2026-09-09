# Stellar / Soroban Developer Tooling

The AI Code Assistant includes Stellar/Soroban developer tooling: safe network
configuration, a read-only Horizon service, a read-only **Stellar RPC**
(Soroban RPC) client, heuristic project detection, contract/account
inspection, and Stellar-aware AI analysis.

> Honest status: everything below is implemented and tested. Anything not
> listed below should be assumed **not implemented yet** (see
> [What is not implemented](#what-is-not-implemented)).

## Why Stellar support exists

Stellar/Soroban developers spend significant time on contract structure,
dependency hygiene, network configuration, and code review. The assistant
helps by:

1. Recognizing a Soroban/Stellar project when it is imported (with explicit
   confidence, and never misclassifying plain Rust).
2. Providing a read-only, SSRF-safe view of live Stellar network data (network
   health, ledgers, accounts, contracts, ledger entries, transactions,
   events).
3. Running AI analyses grounded in the actual contract files, manifests, and
   Stellar configuration.

All Stellar data flows through the existing authorization boundaries: a user
can only analyze projects they own, and the RPC/Horizon clients only read
public network data bound to the configured network.

## What is implemented

### Network configuration (`app/config.py`, `.env.example`)

| Variable                  | Default    | Description                                     |
| ------------------------- | ---------- | ----------------------------------------------- |
| `STELLAR_NETWORK`         | `testnet`  | `mainnet` \| `testnet` \| `futurenet` \| `custom` |
| `STELLAR_HORIZON_URL`     | (preset)   | Override Horizon endpoint                       |
| `STELLAR_RPC_URL`         | (preset)   | Override Stellar RPC endpoint                   |
| `STELLAR_REQUEST_TIMEOUT` | `15`       | Outbound request timeout (seconds)              |
| `STELLAR_MAX_RESPONSE_BYTES` | `2097152` | Cap on response body size                    |
| `STELLAR_RPC_MAX_KEYS`    | `100`      | Max ledger keys per `getLedgerEntries` call    |
| `STELLAR_STRICT_HOST_VALIDATION` | `1` | DNS-verify public hosts resolve publicly     |

Defaults are **testnet**, so nothing touches real XLM unless explicitly
configured. Explicit endpoints are validated before use (see
[Endpoint safety](#endpoint-safety)).

### Network presets (`app/services/stellar.py`)

- `mainnet`, `testnet`, `futurenet` — official public endpoints (Horizon + RPC).
- `local` / `custom` — loopback-only configuration for a local
  `stellar-core` / `stellar-rpc`.

### Per-user network selection (switcher)

Logged-in users can switch the network their read-only Stellar requests and
analysis use from the **Network status** panel on `/stellar` (or via
`GET`/`PUT /stellar/api/network`). Only the **fixed supported networks**
(`testnet`, `mainnet`, `futurenet`, `custom`) are selectable — never a raw URL,
so SSRF/endpoint validation is untouched. The choice is stored per user
(`users.stellar_network`); with no stored selection the operator-configured
`STELLAR_NETWORK` remains authoritative, and **mainnet is never an implicit
default** — it only takes effect after an explicit user selection. The selected
network routes through `StellarService`, `SorobanRpcClient`, and the Stellar
analysis context inside authenticated requests.

### Read-only Horizon service (`app/services/stellar.py::StellarService`)

- `get_network_info()` — network metadata (never contacts the node).
- `validate_address(address)` — structural G-address validation.
- `get_account(address)` — bounded account lookup.
- `get_transaction(hash)` — bounded transaction lookup.
- `get_ledger(sequence)` — bounded ledger lookup.
- `get_assets(cursor, limit)` — bounded issued-asset list.
- `get_account_transactions(address, limit)` — bounded account history.

### Read-only Stellar RPC client (`app/services/soroban_rpc.py`)

`SorobanRpcClient` implements the **read-only** subset of the current Stellar
RPC API (see [docs/soroban.md](soroban.md)): `getHealth`, `getVersionInfo`,
`getLatestLedger`, `getNetwork`, `getLedgerEntries`, `getLedgers`,
`getTransaction`, `getTransactions`, `getEvents`, `getFeeStats`. It never
implements `sendTransaction` or `simulateTransaction`.

### strkey + LedgerKey encoders (`app/services/stellar_xdr.py`)

Minimal, fixture-verified encoders for SEP-23 strkeys (with full CRC16
checksum validation) and the `LedgerKey` values used by `getLedgerEntries`
(account, contract-instance, contract-code). Contract/account inspection
therefore validates addresses properly rather than structurally only.

### Inspection (`app/services/stellar_inspection.py`)

- `network_status()` — configured network + best-effort live RPC health /
  latest ledger (honest when the RPC is unreachable).
- `inspect_account(address)` — parsed account data plus ledger freshness.
- `inspect_contract(contract_id, wasm_hash=None)` — the contract's instance
  ledger entry and (optionally) its deployed wasm metadata. Supported
  contract-data/contract-code entries are decoded into a structured view
  (`instance_entry["decoded"]`, `code_entry["decoded"]`); the raw XDR is
  returned bounded alongside it.
- `inspect_ledger_entry(key)` — a live ledger entry by base64 `LedgerKey`,
  with the same decoded view where the entry type is supported.

Contract-data decoding lives in `app/services/stellar_xdr_decode.py` (bounded
`LedgerKey`/`LedgerEntryData`/`SCVal` decoding, pinned by fixtures): a contract
instance entry decodes into its contract id, durability, executable wasm hash
and storage, and a contract-code entry into its wasm hash + byte size (raw wasm
bytes are never dumped). Unsupported entry types and malformed XDR are reported
explicitly as undecodable — decoded values are never guessed at.

The same module adds a bounded, **read-only transaction-envelope decoder**,
`decode_transaction_envelope(base64_xdr)` (pinned by round-trip fixtures):
V1, legacy V0, and fee-bump envelopes decode into source account, fee (stroops
+ exact lumens), sequence, memo, preconditions, signatures, and operations.
Supported operations are payment, create account, change trust, bump sequence,
invoke host function (contract, function name, argument count, and a bounded
view of the Soroban auth entries — signature bytes are never dumped), extend
footprint TTL, and restore footprint. Amounts are presented in stroops with an
exact lumen string. Any other operation type is reported explicitly as
unsupported; because each operation has a variable-length layout, decoding
stops at that operation and the remaining operations/signatures are honestly
marked *not parsed* rather than guessed at. Malformed/truncated XDR returns an
explicit undecodable result.

### Project detection (`app/services/stellar_detection.py`)

`detect_stellar_project(files)` classifies an indexed project:

- **likely** — Soroban crate in `Cargo.toml`, or `#[contractimpl]` /
  `#[contract]` attributes / `soroban_sdk::` imports in Rust sources.
- **possible** — Stellar SDK dependency (JS/Python/Go), a `stellar.toml` /
  `soroban.toml` / `.soroban` config, a `contracts/` layout, or Stellar/Soroban
  CLI tooling in build/CI files and shell scripts (Makefile/Justfile/Dockerfile,
  `build.rs`/`xtask`, GitHub Actions workflows, GitLab CI (`.gitlab-ci.yml`),
  CircleCI (`.circleci/`), Travis (`.travis.yml`), and `*.sh` scripts). Tooling
  detection is command-fragment based (`soroban contract …`, `stellar xdr …`,
  …) — merely mentioning “Stellar/Soroban” in a README or keyword-only comments
  never triggers it.
- **none** — otherwise. A plain Rust crate is never classified as Soroban.

`detect_stellar_network(files)` extracts a network hint (testnet/mainnet/
futurenet) from config passphrases and file names — never from live data.
Detection metadata is attached to every import response and exposed via
`GET /workspaces/api/projects/<id>/stellar`.

### Stellar-aware AI analysis (`app/services/project_analysis.py`)

Two analysis kinds: **`stellar`** (project overview for a Stellar developer)
and **`stellar_security`** (Soroban-aware security review). Both:

- Run the same content-access gate as every other analysis (owner-only,
  fails closed).
- Report honestly when the project is not Stellar.
- Ground claims in the indexed files, mark `[CONFIRMED]` vs `[SUGGESTION]`,
  and include an honest live-RPC availability note.

#### Structured findings (`app/services/stellar_findings.py`)

The `stellar_security` model is asked to close its narrative with a bounded
JSON findings block. That block is parsed **defensively** — malformed or
missing JSON never crashes or alters the analysis; the narrative is returned
unchanged and nothing is persisted:

- Values are normalized to a shared vocabulary (severity, Stellar category,
  confidence), length-bounded, capped at 50 findings, and never invented from
  prose. `[CONFIRMED]`/`[SUGGESTION]` markers map to `confirmed`/`suggestion`.
- Findings are persisted per project as `StellarSecurityFinding` rows (the
  analysis request commits atomically with its activity row). Re-running an
  analysis replaces the previous run's findings.
- Findings are **evidence, not verdicts** — the narrative itself states the AI
  never claims formal verification or proven vulnerabilities.
- Non-Stellar projects always receive the honest "not applicable" result with
  an empty findings list and nothing is persisted.

Findings ride along in the `stellar_security` analysis API response and are
read back owner-scoped via
`GET /workspaces/api/projects/<id>/stellar/security-findings` (same 404-on-
non-owner gate as every project surface, so there is no existence oracle).
Display in the project developer panel is handled by the results-panel UI.

### UI

- A **Stellar** section in the main navigation (`/stellar`) — a read-only
  developer page with live network status (and explicit per-user network
  selection), account inspection, contract inspection, and ledger-entry
  lookup. Supported contract-data/code entries are decoded into a structured
  view with the bounded raw XDR shown alongside; every lookup has loading,
  empty, error, and timeout states. Shared rendering lives in
  `app/static/js/stellar_tools.js`.
- A **Stellar** tab in the project explorer — per-project detection
  (confidence, evidence, network hints) with the relevant files **linked to
  the file viewer**, plus the same live read-only network status and
  account/contract/ledger-entry lookups so a developer can inspect the project
  and its on-chain data in one place. Detection evidence and relevant files
  come from the indexed project only; the live lookups are bound to the
  configured network and never accept a URL from project content.

### CLI (`flask stellar …`)

`flask stellar network`, `validate <address>`, `account <address>`, `health`,
`contract <id>`, `ledger-entry <key>` — all read-only.

## Endpoint safety

`validate_endpoint_url` enforces:

- `http`/`https` schemes only.
- `https` required for public networks (mainnet/testnet/futurenet), and no
  private/link-local/loopback IP literals or obviously-private hostnames.
- Loopback hosts only (`localhost`, `127.0.0.1`, `::1`) for custom networks.
- Requests are restricted to the configured base URL, **redirects are
  refused**, public hosts are DNS-verified to resolve publicly, and every
  request enforces a timeout and a response-body cap.

Endpoint URLs come exclusively from configuration — never from user input or
project files — so there is no way for an imported project to direct the
service at an arbitrary host.

## How developers use it

1. Import a Soroban/Stellar project (GitHub or archive upload). The import
   response carries detection metadata, and the project explorer's **Stellar**
   tab shows confidence, evidence, network hints, and relevant files.
2. Run **Stellar** or **Stellar Security** analysis on the project. If the
   project is detected as Stellar/Soroban you get a grounded analysis;
   otherwise the tool says it is not applicable.
3. Open **Stellar** in the navigation to inspect the configured network, an
   account (G…), or a contract (C…). Everything is read-only and bound to the
   configured network.
4. From the command line:

```bash
flask stellar network
flask stellar validate G…
flask stellar account G…
flask stellar health
flask stellar contract C…
```

## Stellar-aware GitHub analysis (PR and issue)

The existing GitHub PR and issue AI analyses are **detection-driven** and
Stellar-aware:

- When analyzing a pull request, `analyze_pull_request` runs the existing
  detection (`detect_stellar_project`) over the PR's changed files
  (`filename`/`patch`) plus a small, bounded slice of detection-relevant
  repository files (manifests, Stellar configs, contract layouts, CLI-tooling
  files). The repo slice is fetched through the user's own GitHub token and is
  capped, so nothing private or unbounded is pulled in.
- When analyzing an issue, `analyze_issue` runs the same detection over the
  same bounded repo slice.
- If the evidence yields a Stellar/Soroban confidence of `possible` or
  `likely`, a **bounded, clearly-labelled Stellar context block** (confidence,
  signals/evidence, network hints, relevant files, changed files) and
  Stellar-aware review guidance are appended to the prompt. The guidance covers
  authorization/access control, contract/admin authority, cross-contract
  calls, storage/TTL handling, `panic!`/`unwrap!`, network/config mistakes,
  secret/key exposure, and unsafe contract-state assumptions — restricted to
  what the diff/context actually supports.
- If detection is `none` (including **plain Rust** without Soroban evidence, or
  a repo that merely mentions "Stellar"), the existing generic PR/issue
  analysis runs unchanged with **no** Stellar instructions.
- A failed detection never breaks the analysis: it falls back to the generic
  path. No manual `stellar=true` flag exists — behaviour is always derived from
  detection.

No-fabrication policy: the Stellar guidance explicitly forbids claiming formal
verification, deployed contracts, succeeded transactions, or live ledger/RPC
state. PR/issue text and repository content are framed as untrusted data so
they cannot redefine the system instructions. Authorization is unchanged:
GitHub calls stay user-token-scoped and analysis never grants additional
access.

## Local development

- Point `STELLAR_NETWORK=custom` and `STELLAR_HORIZON_URL` /
  `STELLAR_RPC_URL` at a local `stellar-core`/`stellar-rpc` (loopback only).

### Offline mock network (`app/services/stellar_mock.py`)

For tests and local development with **no** Stellar node and **no** external
network, boot the deterministic offline mock:

```python
from app.services.stellar_mock import MockStellarServer

server = MockStellarServer().start()
print(server.horizon_url, server.rpc_url)  # -> http://127.0.0.1:<port>[/rpc]
# ...point a custom network config at server.horizon_url / server.rpc_url...
server.stop()
```

or run it as a standalone process for manual local development:

```bash
python -m app.services.stellar_mock
```

The mock serves a fixed fixture set over the same two APIs the app reads:
Horizon-style JSON (`/accounts/...`, `/transactions/...`, `/ledgers/...`,
`/assets`) and the read-only Stellar RPC JSON-RPC method set on `/rpc`
(health, version, latest ledger, network, ledger entries, ledgers,
transactions, events, fee stats). It is read-only, deterministic, and never
touches a real network. In pytest it is available via the session-scoped
`mock_stellar` fixture; see `tests/test_stellar_mock_network.py` for an
end-to-end example driving the real `StellarService` and `SorobanRpcClient`.

- The test suite uses deterministic fixtures and mocked transport (including
  the in-process mock network); no real network access is required.

## What is not implemented

- Transaction signing/submission, wallets, or custodial features (out of scope
  by design — this is developer tooling).
- An account dashboard and a contract-data browsing UI (open contributor
  issues).
- `sendTransaction` / `simulateTransaction`.

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) (Stellar/Soroban section) and
[docs/soroban.md](soroban.md). Stellar work is tracked under the Phase 8
milestone with the `stellar`/`soroban` labels. Keep the SSRF guards, the
read-only invariant, and the honest-claims rule in mind on every change.
