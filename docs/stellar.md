# Stellar / Soroban Developer Tooling

This project includes a **foundation** for Stellar/Soroban developer tooling:
safe network configuration, a read-only Stellar service, heuristic project
detection, and a Stellar-aware AI analysis. It does **not** yet implement
transaction signing, funds transfer, XDR inspection, or a full Soroban RPC
client — those are tracked as contributor work under the Phase 8 milestone.

> Honest status: this is a real, tested foundation. Anything not listed below
> as implemented should be assumed **not implemented yet**.

## Why Stellar support exists

The AI Code Assistant is a developer productivity tool. Stellar/Soroban
developers spend significant time on contract structure, dependency hygiene,
network configuration, and code review. The foundation here lets the assistant:

1. Recognize a Soroban/Stellar project when it is imported.
2. Provide a read-only, SSRF-safe view of Stellar network data.
3. Run an AI analysis grounded in the actual contract files and manifests.

All Stellar data flows through the existing Phase 7 authorization boundaries:
a user can only analyze projects they own, and nothing is ever sent to the AI
that the user is not already authorized to see.

## What is implemented

### Network configuration (`app/config.py`, `.env.example`)

| Variable                  | Default                                    | Description                           |
| ------------------------- | ------------------------------------------ | ------------------------------------- |
| `STELLAR_NETWORK`         | `testnet`                                  | `mainnet` \| `testnet` \| `futurenet` \| `custom` |
| `STELLAR_HORIZON_URL`     | (preset for network)                       | Override Horizon endpoint             |
| `STELLAR_RPC_URL`         | (preset for network)                       | Override Soroban RPC endpoint         |
| `STELLAR_REQUEST_TIMEOUT` | `15`                                       | Outbound request timeout (seconds)    |
| `STELLAR_MAX_RESPONSE_BYTES` | `2097152`                               | Cap on response body size             |

Defaults are testnet, so nothing touches real XLM unless explicitly
configured. Explicit endpoints are validated before use (see
[Endpoint safety](#endpoint-safety)).

### Network presets (`app/services/stellar.py`)

- `mainnet`, `testnet`, `futurenet` — official public endpoints.
- `local` — loopback-only configuration for a local `stellar-core` /
  `soroban-rpc`.

### Read-only Stellar service (`app/services/stellar.py::StellarService`)

- `get_network_info()` — network metadata (never contacts the node).
- `validate_address(address)` — structural G-address validation.
- `get_account(address)` — bounded account lookup from Horizon.
- `get_transaction(hash)` — bounded transaction lookup from Horizon.

The service never signs, sends, or funds transactions.

### Project detection (`app/services/stellar_detection.py`)

`detect_stellar_project(files)` heuristically classifies an indexed project:

- **likely** — Soroban crate in `Cargo.toml`, or `#[contractimpl]` /
  `#[contract]` attributes / `soroban_sdk::` imports in Rust sources.
- **possible** — Stellar SDK dependency (JS/Python/Go), a `stellar.toml` /
  `soroban.toml` / `.soroban` config, or a `contracts/` layout.
- **none** — otherwise. A plain Rust crate is never classified as Soroban.

### Stellar-aware AI analysis (`app/services/project_analysis.py`)

A new analysis kind, `stellar`, is available on the project analyzer:

- Runs the same content-access gate as every other analysis (owner-only,
  fails closed).
- If no Stellar signals are detected, it says so explicitly — it never
  fabricates Stellar claims.
- Otherwise it assembles bounded context from the contract sources, manifests,
  and Stellar configuration and asks the model for: project kind, contract
  responsibilities, configured networks/endpoints, dependency purpose, and
  concrete contract-specific risks (panics, unwraps, auth checks, tests).

The prompt instructs the model to never claim a Stellar integration,
deployment, or partnership that the files do not demonstrate.

## Endpoint safety

`validate_endpoint_url` enforces:

- `http`/`https` schemes only.
- `https` required for public networks (mainnet/testnet/futurenet).
- Loopback hosts only (`localhost`, `127.0.0.1`, `::1`) for custom/development
  networks.
- Requests are also restricted to the configured Horizon base URL, and enforce
  a timeout and a response-body cap.

Endpoint URLs come exclusively from configuration — never from user input or
from project files — so there is no way for an imported project to direct the
service at an arbitrary host.

## How developers use it

1. Import a Soroban project (GitHub or archive upload).
2. Run **Stellar** analysis on the project explorer. If the project is
   detected as Stellar/Soroban you get a grounded analysis; otherwise the tool
   says it is not applicable.
3. Read network metadata or validate addresses in code:

```python
from app.services.stellar import get_stellar_service

service = get_stellar_service()
print(service.get_network_info())
print(service.validate_address("G..."))
```

## Contributing

Planned contributor work (Phase 8 milestone, label `stellar`/`soroban`):

- Soroban RPC integration (getContractData, getLedgerEntries, simulate…).
- Horizon improvements (asset/ledger/operation endpoints).
- XDR transaction/contract inspection.
- Stellar account tooling and network switcher UI.
- Stellar project templates and a "new Soroban contract" workflow.
- Deeper Stellar-specific AI prompts and security analysis.
