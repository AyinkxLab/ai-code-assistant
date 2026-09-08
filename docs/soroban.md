# Soroban / Stellar RPC Developer Guide

This document describes the read-only Soroban/Stellar RPC layer added to the AI
Code Assistant: what it does, which methods it implements, how it stays safe,
what it deliberately does **not** do, and how contributors can extend it.

> Honest status: this is a real, tested, read-only RPC client. It never signs,
> simulates, or submits transactions. Supported contract-data/contract-code XDR
> is decoded into a structured, bounded view; anything unsupported or malformed
> is reported explicitly and never guessed at.

## Why an RPC layer exists

Horizon gives parsed account/transaction data, but Stellar RPC is the
recommended real-time interface for the Stellar network and the *only* interface
for live smart-contract (Soroban) state. The docs describe Horizon as *nearing
end-of-life*. This project therefore adds a bounded, SSRF-safe client for the
**read-only** subset of Stellar RPC so developers can inspect network health,
ledgers, contracts, ledger entries, transactions, events, and fee statistics
without leaving the assistant.

## Version assumptions

- **RPC API:** current Stellar RPC (renamed from *Soroban RPC* in Nov 2024).
- **XDR:** `stellar-xdr` v22 (the method set used by Stellar Core protocol 22+).
- The current method set does **not** include `getContractData`/`getContractCode`
  as standalone methods — contract data and code are read through
  `getLedgerEntries` with `LedgerKey::ContractData` / `LedgerKey::ContractCode`.
- Reference: <https://developers.stellar.org/docs/data/apis/rpc/api-reference/methods>

## Implemented methods

All methods are **read-only** and bounded:

| Method                  | Purpose                                          |
| ----------------------- | ------------------------------------------------ |
| `getHealth`             | Node health and the ledger retention window      |
| `getVersionInfo`        | RPC server version / build info                  |
| `getLatestLedger`       | Latest known ledger (id, protocol, sequence)     |
| `getNetwork`            | Network passphrase and protocol version          |
| `getLedgerEntries`      | Live ledger entries by base64 `LedgerKey`        |
| `getLedgers`            | Bounded list of recent ledgers                   |
| `getTransaction`        | A transaction by hash (SUCCESS/FAILED/NOT_FOUND) |
| `getTransactions`       | Bounded list of recent transactions              |
| `getEvents`             | Filtered contract/system events over a range     |
| `getFeeStats`           | Inclusion fee statistics                         |

Deliberately **not** implemented: `sendTransaction` and `simulateTransaction`
(they concern signing/submission).

## Module layout

- `app/services/soroban_rpc.py` — `SorobanRpcClient` (JSON-RPC 2.0 transport +
  the methods above) and the `SorobanRpc*` error taxonomy.
- `app/services/stellar_xdr.py` — minimal, verified strkey (SEP-23) and
  `LedgerKey` XDR encoders used to build keys for `getLedgerEntries`
  (account, contract-instance, contract-code). Tested against authoritative
  fixtures (including an exact match for the official docs' account key).
- `app/services/stellar_inspection.py` — developer-facing `inspect_account`,
  `inspect_contract`, `inspect_ledger_entry`, `network_status`.
- `app/services/stellar_xdr_decode.py` — bounded, read-only decoder for the
  base64 `LedgerKey`/`LedgerEntryData`/`SCVal` values `getLedgerEntries`
  returns (contract-data instances, symbol keys, contract-code metadata,
  `SCVal`/`SCAddress` values), pinned by spec-derived fixtures.
- `app/stellar/` — the `/stellar` page and its read-only APIs.
- `app/services/stellar_cli.py` — `flask stellar …` commands.

## Building ledger keys

`getLedgerEntries` requires base64 `LedgerKey` values. The project provides
verified encoders for the keys it supports:

```python
from app.services.stellar_xdr import (
    ledger_key_for_contract,   # ContractData instance key (persistent)
    ledger_key_contract_code,  # ContractCode key from a wasm hash
    ledger_key_for_account,    # Account key from a G-address
)
```

Contract/account addresses are validated with **full strkey checksum**
verification (SEP-23, CRC16-XMODEM), which is stronger than the structural
`G`-address check used by the base `StellarService`. Caller-supplied raw keys
are validated structurally (base64 + length) before being sent.

## Example

```python
from app.services.soroban_rpc import get_soroban_rpc_client

client = get_soroban_rpc_client()          # bound to STELLAR_NETWORK config
print(client.get_health())                 # node health + retention window
print(client.get_latest_ledger())          # latest ledger
```

Inspect a contract (instance entry + optional wasm metadata):

```python
from app.services.stellar_inspection import inspect_contract
result = inspect_contract("C…contract id", wasm_hash="…64 hex chars…")
print(result["found"], result["instance_entry"])
```

## Decoding contract data

Each `getLedgerEntries` result carries a base64 `LedgerKey` (`key`), the entry's
data (`xdr`, a `LedgerEntryData`), and its ledger metadata. The decoder
(`app/services/stellar_xdr_decode.py`) turns supported values into a bounded,
JSON-safe, developer-facing view:

- `decode_ledger_key(value)` — account, contract-data, and contract-code keys.
- `decode_ledger_entry_data(value)` — `CONTRACT_DATA` entries (contract
  address, `SCVal` key, durability, `SCVal` value incl. a contract instance's
  executable wasm hash and storage) and `CONTRACT_CODE` entries (wasm hash +
  byte size; the wasm bytes are never dumped).
- `decode_scval_xdr(value)` — a standalone `SCVal` (e.g. an event topic/value).

Decoded values always carry a `decoded: true/false` flag. Unsupported entry
types (e.g. classic `ACCOUNT`) and malformed XDR return `decoded: false` with a
reason — decoding never fabricates a value, and raw wasm bytes are never copied
into results. `inspect_contract` / `inspect_ledger_entry` attach this decoded
view (via `instance_entry["decoded"]`, `code_entry["decoded"]`,
`entry["decoded"]`) beside the bounded raw XDR.

## Network configuration

RPC endpoints come **exclusively** from validated configuration:

- `STELLAR_NETWORK=testnet` (default) → `https://soroban-testnet.stellar.org`
- `mainnet` → `https://soroban-mainnet.stellar.org`
- `futurenet` → `https://soroban-futurenet.stellar.org`
- `custom` → loopback-only endpoints (a local `stellar-rpc`), e.g.
  `http://127.0.0.1:8000`

Operators may override the RPC endpoint with `STELLAR_RPC_URL`, subject to the
same endpoint validation (https for public networks, loopback-only for custom).
There is **no** way for a user or an imported project to supply an RPC URL.

### Per-user network selection

Logged-in users can switch the network their read-only Stellar requests and
analysis use (see the **Network status** panel on the `/stellar` page and
`PUT /stellar/api/network`). Only the **fixed supported networks** are
selectable — `testnet`, `mainnet`, `futurenet`, and `custom` (local /
development) — never a raw URL. Validation rejects anything else before it can
be stored.

- An unset selection keeps the operator-configured `STELLAR_NETWORK`
  authoritative.
- **Mainnet is never an implicit default.** With the shipped configuration the
  default is `testnet`; mainnet only takes effect after a user explicitly
  selects it.
- The stored selection is per user, applies within an authenticated request,
  and routes `StellarService`, `SorobanRpcClient`, and the Stellar analysis
  context through the chosen network. It grants no additional access and does
  not weaken any endpoint/SSRF validation.

> Related: the GitHub PR/issue analyses are **detection-driven** and
> Stellar-aware without calling the RPC. They reuse `stellar_detection` over
> the changed files / a bounded repo slice and add bounded Stellar review
> guidance; they never claim live ledger/RPC state (see
> [docs/stellar.md](stellar.md)).

## Security model

- **Read-only.** The client implements no signing or submission path.
- **SSRF-safe.** Endpoints come from configuration, are restricted to the
  configured base URL, refuse redirects, and (for public networks) are
  verified to resolve to a globally routable address before each request.
- **Bounded.** Every request enforces a timeout (`STELLAR_REQUEST_TIMEOUT`),
  a response-size cap (`STELLAR_MAX_RESPONSE_BYTES`), a maximum of
  `STELLAR_RPC_MAX_KEYS` ledger keys per call, and result/raw-XDR length caps.
- **Fail closed.** Malformed JSON, non-JSON-RPC payloads, id mismatches, HTTP
  errors, and RPC error payloads all raise typed errors; nothing is silently
  ignored.
- **Honest about XDR.** Supported contract-data/contract-code XDR returned by
  `getLedgerEntries` is decoded into a structured, bounded view (see
  `stellar_xdr_decode.py`). Unsupported entry types and malformed XDR are
  reported explicitly as undecodable; the project never guesses at decoded
  values and never dumps raw wasm bytes.

## Known limitations / gaps (open contributor work)

- A UI for browsing decoded contract data and ledger entries (tracked in #185).
- `getLedgerEntries` durable-key pagination, and symbol-key lookups.
- A mock RPC server for local development (#182).
- Durable pagination and cursor support for all methods.

## Contributing

- Run the suite:
  `pytest tests/test_soroban_rpc.py tests/test_stellar_xdr.py tests/test_stellar_xdr_decode.py`
- Keep the SSRF/redirect/`_check_url` guards intact — new methods must go
  through `_rpc_call`.
- Verify XDR discriminants against `stellar-xdr` before changing encoders or
  the decoder; both are pinned by fixture tests.
