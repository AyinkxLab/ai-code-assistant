# Soroban developer workflow (end to end)

This is a hands-on walkthrough for a Soroban developer using the assistant:
import a Soroban repository, see how it is detected, run Stellar-aware analysis,
interpret the confidence and evidence, inspect live network data read-only, and
generate a new contract scaffold.

> Honest status: every step below is **implemented and tested** unless it is
> explicitly marked **(planned)**. The scaffold step is implemented as an API
> but has **no UI yet**. See
> [What is not implemented](#what-is-not-implemented) for the full list.

Related reading: [docs/stellar.md](stellar.md) (tooling reference) and
[docs/soroban.md](soroban.md) (read-only RPC layer and scaffold details).

## Prerequisites

- You are signed in and can access the target workspace. Workspaces are resolved
  server-side; a workspace you are not a member of is reported as "not found"
  (no existence oracle).
- The default network is `testnet` (`STELLAR_NETWORK`), so nothing touches real
  XLM unless an operator deliberately configures mainnet. See
  [docs/stellar.md](stellar.md#network-configuration-appconfigpy-envexample).

## Step 1 — Import a Soroban repository

### In the web UI

1. Open **Workspaces** in the top navigation (`/workspaces/`) and create or open
   a workspace.
2. In the workspace page, under **Import a project** you can:
   - **Upload** an archive (`.zip`, `.tar`, `.tar.gz`, `.tgz`, `.7z`, `.rar`), or
   - paste an `owner/name` GitHub repository and click **Import from GitHub**.

Once the import finishes, the import feedback shows the result. A detected
Stellar/Soroban project gets a confidence badge with links to open the project
or jump straight to its **Stellar** tab (`?tab=stellar`); non-Stellar imports
behave exactly as before.

### From the API

All three import forms share one endpoint:
`POST /workspaces/api/workspaces/<workspace_id>/projects`.

| Source          | Request                                                                   |
| --------------- | ------------------------------------------------------------------------- |
| Archive upload  | `multipart/form-data` with a file field named `file`                       |
| GitHub          | JSON `{"repo": "owner/name"}`                                             |
| Soroban scaffold| JSON `{"source": "scaffold", "name": "My Token"}` (see Step 5)            |

```bash
# Archive upload
curl -b cookies.txt -F "file=@my-soroban-repo.zip" \
  https://<host>/workspaces/api/workspaces/1/projects

# GitHub import
curl -b cookies.txt -H "Content-Type: application/json" \
  -d '{"repo": "stellar/soroban-examples"}' \
  https://<host>/workspaces/api/workspaces/1/projects
```

A **synchronous** response is the project object plus a top-level `stellar`
detection object:

```json
{
  "id": 12,
  "workspace_id": 1,
  "name": "my-soroban-repo",
  "source": "archive",
  "status": "ready",
  "file_count": 42,
  "stellar": {
    "is_stellar": true,
    "is_soroban": true,
    "confidence": "likely",
    "signals": { "...": "see Step 2" },
    "network_hints": ["testnet"],
    "relevant_files": ["Cargo.toml", "src/lib.rs"],
    "evidence": ["soroban-sdk dependency in Cargo.toml"],
    "contract_entry_point": "src/lib.rs"
  }
}
```

> Async imports: archive and GitHub imports use an in-process background worker
> by default (`IMPORT_JOBS_ASYNC=1`). The immediate response then has
> `status: "indexing"` and **no** `stellar` object. Poll the project (or open its
> **Stellar** tab) after indexing completes. Scaffold imports are always
> synchronous.

## Step 2 — See detection (confidence, evidence, network hints)

### In the web UI

Open the project and select the **Stellar** tab (deep link `?tab=stellar`).
The panel shows **Confidence**, **Stellar**, **Soroban (smart contracts)**,
**Network hint**, **Contract entry point**, **Evidence**, and **Relevant Stellar
files** (each linked to the file viewer). The relevant files and evidence come
only from the indexed project.

### From the API

`GET /workspaces/api/projects/<project_id>/stellar` returns the detection plus
the extracted network hint:

| Field                 | Meaning                                                                 |
| --------------------- | ----------------------------------------------------------------------- |
| `is_stellar`          | Any Stellar/Soroban signal found.                                        |
| `is_soroban`          | A smart-contract signal was found.                                       |
| `confidence`          | `none` \| `possible` \| `likely` (rules below).                          |
| `signals`             | Eight booleans, one per detection rule (below).                          |
| `network_hints`       | Networks mentioned in config/passphrases/file names (never live data).   |
| `relevant_files`      | The files that produced the signals.                                     |
| `evidence`            | Human-readable reasons, one per matching signal.                         |
| `network`             | `{"network": "testnet" \| ..., "evidence": [...]}`                       |
| `network_files`       | Config/`.soroban` files (present only when `is_stellar`).                |
| `contract_entry_point`| Contract `src/lib.rs` path (present only when `is_soroban`).             |

### How confidence is decided

`confidence` is derived, never guessed:

- **`likely`** — any of `soroban_cargo_dependency`, `soroban_attribute`,
  `soroban_import`.
- **`possible`** — any of `stellar_sdk_dependency`, `stellar_config_file`,
  `soroban_config_dir`, `contract_directory`, `stellar_cli_tooling`.
- **`none`** — otherwise. A **plain Rust crate is never classified as Soroban**.

The `signals` booleans are: `soroban_cargo_dependency`, `soroban_attribute`,
`soroban_import`, `stellar_sdk_dependency`, `stellar_config_file`,
`soroban_config_dir`, `contract_directory`, `stellar_cli_tooling`. Detection is
recomputed from the indexed files on every request; it is **not** stored on the
project row.

## Step 3 — Run Stellar analysis

### In the web UI

Open the project's **Analysis** tab and run either **Stellar** (project overview
for a Stellar developer) or **Stellar Security** (Soroban-aware security
review). The narrative is rendered in the results panel, with `[CONFIRMED]` and
`[SUGGESTION]` markers shown as tags.

### From the API

`POST /workspaces/api/projects/<project_id>/analyze` with a JSON body:

```json
{ "kind": "stellar" }
```

`kind` may be `stellar` or `stellar_security` (other kinds such as
`architecture` or `security` also exist). An unsupported kind returns HTTP 400.

Detected projects get a grounded response. `stellar` returns:

| Field        | Meaning                                                          |
| ------------ | ---------------------------------------------------------------- |
| `kind`       | `"stellar"`.                                                      |
| `detected`   | `true`.                                                           |
| `confidence` | Detection confidence (`possible`/`likely`).                      |
| `is_soroban` | Whether contract signals were found.                             |
| `network`    | `{network, evidence}` network hint.                              |
| `analysis`   | The narrative (facts marked `[CONFIRMED]` / `[SUGGESTION]`).     |

`stellar_security` returns `kind`, `detected`, `confidence`, `is_soroban`,
`analysis`, `findings`, `findings_count`, `structured`, and `persisted_count`.

A project that is not detected as Stellar gets an explicit "not applicable"
result (`detected: false`) instead of a fabricated review, and nothing is
persisted.

> The narrative is returned as the `analysis` field (there is no `narrative`
> key). The live-RPC availability note is embedded in the prompt, not returned
> as a separate field.

## Step 4 — Interpret confidence and evidence

There are **two different confidence concepts** — don't mix them:

1. **Detection confidence** (`none`/`possible`/`likely`) — how sure the
   assistant is that the *project* is Stellar/Soroban (Step 2).
2. **Finding confidence** (`confirmed`/`potential`/`suggestion`) — how strongly
   a single security finding is supported by evidence.

Within the narrative:

- `[CONFIRMED]` marks facts grounded in the indexed files.
- `[SUGGESTION]` marks hypotheses or recommendations.

Structured `stellar_security` findings (each carrying `file`, `line`,
`severity`, `category`, `confidence`, `evidence`, `explanation`,
`recommendation`) are persisted per project and read back owner-only:

```http
GET /workspaces/api/projects/<project_id>/stellar/security-findings
```

The response is `{"project_id": ..., "count": N, "findings": [...]}`.
Severities are `critical|high|medium|low|informational`; categories include
`authorization`, `admin-controls`, `error-handling`, `cross-contract`,
`secrets`, `storage`, `testing`, `configuration`, `other`.

Findings are **evidence, not verdicts**: the assistant never claims formal
verification or proven vulnerabilities, and re-running an analysis replaces the
previous run's findings.

> UI caveat: the project **Analysis** panel renders only the narrative
> (`analysis`). The structured `findings` array is available through the
> endpoint above (and persisted rows); rendering it in the panel is
> **(planned)**.

## Step 5 — Scaffold a new contract (implemented; API-only)

The scaffold is implemented but has **no UI control yet** — call the import
endpoint directly with `source: "scaffold"`:

```json
POST /workspaces/api/workspaces/<workspace_id>/projects
{ "source": "scaffold", "name": "My Token" }
```

The `name` is normalized into a valid Cargo package name (default
`hello_world`). The generated project contains five files:

- `Cargo.toml` (with `soroban-sdk`),
- `src/lib.rs` (`#[contract]` / `#[contractimpl]` hello contract),
- `.soroban/config.toml` (testnet config for the Soroban CLI),
- `README.md`,
- `.gitignore`.

Honesty rules for the scaffold:

- **Generation only.** No `cargo` is executed and no network call is made; the
  generated project is documented as *not compiled/verified*.
- **No secrets.** No keys or deploy accounts are generated.
- The scaffold is automatically detected as **`likely`** Soroban (its manifest
  and source contain the expected signals), so it flows through the same
  detection and analysis surfaces as an imported repository.

## Step 6 — Inspect live network data (read-only)

Use the top-level **Stellar** page (`/stellar`) — or the project **Stellar**
tab — to look up an account (`G…`), a contract (`C…`), or a base64 ledger key,
and to switch networks (`testnet`/`mainnet`/`futurenet`/`custom`). Everything is
read-only, bound to configuration-derived endpoints, and never accepts a URL
from project content. See [docs/stellar.md](stellar.md#what-is-implemented).

## What is not implemented

The following are **not** part of the workflow above; treat anything unlisted as
not implemented.

- **(planned)** A UI control to generate the Soroban scaffold (the API exists).
- **(planned)** Rendering structured `stellar_security` findings in the Analysis
  panel (the findings API and persisted rows exist).
- Detection is recomputed per request and is not persisted on the project.
- Async archive/GitHub imports do not include detection metadata in the initial
  response or the project list; read it from the Stellar endpoint after
  indexing.
- No contract build, deploy, signing, or transaction submission — by design.
  See [docs/stellar.md](stellar.md#what-is-not-implemented).

## See also

- [docs/soroban.md](soroban.md) — read-only Stellar RPC client, XDR decoding,
  and scaffold internals.
- [docs/stellar.md](stellar.md) — the full Stellar/Soroban tooling reference.
- [docs/security.md](security.md) — authorization, SSRF, and honest-claims
  posture.
