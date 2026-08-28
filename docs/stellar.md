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
  ledger entry and (optionally) its deployed wasm metadata. Raw XDR is
  returned bounded and marked **not decoded**.
- `inspect_ledger_entry(key)` — a live ledger entry by base64 `LedgerKey`.

### Project detection (`app/services/stellar_detection.py`)

`detect_stellar_project(files)` classifies an indexed project:

- **likely** — Soroban crate in `Cargo.toml`, or `#[contractimpl]` /
  `#[contract]` attributes / `soroban_sdk::` imports in Rust sources.
- **possible** — Stellar SDK dependency (JS/Python/Go), a `stellar.toml` /
  `soroban.toml` / `.soroban` config, a `contracts/` layout, or Stellar/Soroban
  CLI tooling in build/CI files.
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

### UI

- A **Stellar** section in the main navigation (`/stellar`) with network
  status, account inspection, and contract inspection (read-only).
- A **Stellar** tab in the project explorer showing per-project detection,
  confidence, evidence, and relevant files.

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
- The test suite uses deterministic fixtures and mocked transport; no real
  network access is required.

## What is not implemented

- Transaction signing/submission, wallets, or custodial features (out of scope
  by design — this is developer tooling).
- Full XDR/`SCVal` decoding of contract data into a human-readable view
  (tracked as contributor work).
- A network switcher UI, an account dashboard, a contract-data browsing UI,
  and a mock RPC server (open contributor issues).
- `sendTransaction` / `simulateTransaction`.

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) (Stellar/Soroban section) and
[docs/soroban.md](soroban.md). Stellar work is tracked under the Phase 8
milestone with the `stellar`/`soroban` labels. Keep the SSRF guards, the
read-only invariant, and the honest-claims rule in mind on every change.
