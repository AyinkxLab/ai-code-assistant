# Stellar / Soroban detection design

This document explains the heuristic detector in
[`app/services/stellar_detection.py`](../app/services/stellar_detection.py): its
signal model, the confidence semantics, why a plain Rust project is never
classified as Soroban, and how to extend it safely. It is written to match the
code exactly — if you change the detector, update this document in the same PR.

## Principles

- **Evidence-based.** A project is only classified as Stellar/Soroban when a
  concrete, file-level signal exists (a dependency, an attribute/import, a
  configuration file, a contract layout, or real CLI usage).
- **Conservative.** Layout alone is weak evidence, plain Rust is not evidence,
  and a bare mention of the word "stellar" is never a signal.
- **Read-only and offline.** The detector only reads `ProjectFile` rows the
  caller already holds. It never makes network calls, never executes tooling,
  and never guesses.
- **Explicit uncertainty.** The result always includes `confidence`
  (`none`/`possible`/`likely`) and the `evidence` that produced it.

## Signal model

Detection walks every file and sets signals in `StellarSignals`. Layout signals
are recorded even when a file's content is unavailable; content signals require
the content.

| Signal | Set when | Read from |
| ------ | -------- | --------- |
| `soroban_cargo_dependency` | A dependency name is in `SOROBAN_CRATES` (`soroban-sdk`, `soroban-auth`, `soroban-token-sdk`, `soroban-spec`, `soroban-cli`, `soroban-env-host`, `soroban-rpc`, `stellar-strkey`, `stellar-contract-sdk`, `stellar-contract-env-host`, `stellar-contract-env`, `stellar-xdr`). | Manifest files (see below) |
| `soroban_attribute` | A Rust source contains one of `#[contractimpl]`, `#[contract]`, `#[contracttype]`, `#[contracterror]`. | `*.rs` content |
| `soroban_import` | A Rust source contains one of `soroban_sdk::`, `soroban::`, `soroban_sdk;`. | `*.rs` content |
| `stellar_sdk_dependency` | A dependency name is in `STELLAR_SDK_DEPENDENCIES` (`stellar-sdk`, `js-stellar-sdk`, `@stellar/stellar-sdk`, `@stellar/stellar-base`, `stellar-base`, `py-stellar-base`, `go-stellar-base`, `stellar-client`, `stellar-java-sdk`). | Manifest files |
| `stellar_config_file` | A file's basename is in `_STELLAR_CONFIG_FILES`: `stellar.toml`, `soroban.toml`, `stellar-config.toml`, `stellar.json`, `soroban.json`. | Path |
| `soroban_config_dir` | The path contains `.soroban`. | Path |
| `contract_directory` | The path starts with `contracts/`, `src/contracts/`, or `contract/`. | Path |
| `stellar_cli_tooling` | A build/CI file type **and** its content contains a real CLI fragment. | See below |
| `network_hints` | A filename or passphrase marker identifies a network. | Config files / `.soroban` paths only |

### Manifest dependency parsing

`_manifest_dependency_names` selects a parser by basename:

- `Cargo.toml` — names under `[dependencies]`, `[dev-dependencies]`, or
  `[workspace.dependencies]`.
- `package.json` / `package-lock.json` — `dependencies`, `devDependencies`,
  `peerDependencies`.
- `requirements*.txt` — one requirement per line, version specifiers stripped.
- `go.mod` — `require` entries (module basename).
- `pyproject.toml` — `[project].dependencies` / `optional-dependencies`, or a
  top-level `dependencies` section.
- `Gemfile` — `gem "name"` lines.

Dependency names are matched exactly against the two sets above, so a similarly
named but different crate does not trigger a signal.

### CLI tooling

`_is_cli_tooling` accepts `Makefile`, `justfile`, `Dockerfile`, `build.rs`,
`xtask`, `.gitlab-ci.yml`, `.travis.yml`, workflow YAML under `.github/workflows/`
or `.circleci/`, and `*.sh`. The signal is only set when the file's content also
contains a narrow command fragment from `_CLI_COMMAND_MARKERS` (for example
`soroban contract`, `soroban build`, `soroban invoke`, `soroban deploy`,
`stellar contract`, `stellar rpc`, `stellar-cli`). A shell script that merely
exists, or a CI file that never invokes the CLI, is not a signal.

## Confidence semantics

`StellarSignals.confidence` is computed from the signals, in this exact order:

| Confidence | Rule |
| ---------- | ---- |
| `likely` | `soroban_cargo_dependency` **or** `soroban_attribute` **or** `soroban_import` |
| `possible` | otherwise, if `stellar_sdk_dependency` **or** `stellar_config_file` **or** `soroban_config_dir` **or** `contract_directory` **or** `stellar_cli_tooling` |
| `none` | otherwise |

Derived flags:

- `is_stellar` is `confidence != "none"`.
- `is_soroban` is exactly `soroban_cargo_dependency or soroban_attribute or
  soroban_import` — the strong smart-contract signals.

`to_dict()` returns `is_stellar`, `is_soroban`, `confidence`, the per-signal
booleans under `signals`, `network_hints` (deduped, max 10), `relevant_files`
(deduped, max 20), and `evidence` (max 20). `project_stellar_metadata()` adds
`network_files` when the project is Stellar.

## Why plain Rust is excluded

A Rust project with `.rs` files and a `Cargo.toml` is extremely common and says
nothing about Stellar. Its files set none of the strong signals:

- `Cargo.toml` without a `SOROBAN_CRATES` dependency does not set
  `soroban_cargo_dependency` (dependency names are matched exactly).
- `.rs` files without `#[contract*]` attributes or `soroban_sdk::` imports do
  not set `soroban_attribute` / `soroban_import`.
- There is no config file, no `.soroban` path, and no `contracts/` layout, so no
  weak signal is set either.

The result is `confidence = "none"` and `is_soroban = false`. This is asserted
in `tests/test_stellar_detection.py::TestDetectionBasics::test_plain_rust_project_not_stellar`.
Even a `contracts/` directory on its own is only `possible` (weak layout
evidence), never `likely`.

## Network hints

Network hints are advisory and are only extracted from Stellar config files or
`.soroban` paths (`_network_hints_for_content`):

- Filename markers (`_NETWORK_FILENAME_MARKERS`): `stellar-mainnet`,
  `soroban-mainnet`, `pubnet`, `mainnet`, `stellar-testnet`,
  `soroban-testnet`, `testnet`, `stellar-futurenet`, `soroban-futurenet`,
  `futurenet`.
- Passphrase markers (`_NETWORK_PASSPHRASE_MARKERS`), matched case-insensitively:
  - mainnet — `public global stellar network`, `stellar:pubnet`,
    `public network ; september 2015`
  - testnet — `test sdf network`, `stellar:testnet`,
    `testnet ; september 2015`
  - futurenet — `future network`, `futurenet`
  - standalone — `standalone network`, `local network`, `stellar:standalone`

`detect_stellar_network(files)` returns a single network only when the evidence
is unambiguous; conflicting or absent hints return `network: null`. Hints are
never treated as live network data.

## Adapters for repository / diff contexts

The same detector runs against non-`ProjectFile` inputs through thin adapters;
they never re-implement detection:

- `detection_relevant_paths(paths, limit=25)` selects only the paths that can
  carry evidence (manifests, config files, contract-layout paths, CLI-tooling
  files, and `.soroban` paths) so a bounded content fetch can focus on them.
- `detect_stellar_from_dicts(rows)` adapts dict rows that use either
  `path`/`content` or `filename`/`patch` (e.g. PR changed files).

## Adding a new signal safely

1. **Pick the right bucket.** Add exact names to `SOROBAN_CRATES` /
   `STELLAR_SDK_DEPENDENCIES`, attributes to `_SOROBAN_ATTRIBUTES`, imports to
   `_SOROBAN_IMPORTS`, config basenames to `_STELLAR_CONFIG_FILES`, or command
   fragments to `_CLI_COMMAND_MARKERS`. Add a new `StellarSignals` field only if
   none of the existing buckets expresses the evidence.
2. **Keep it narrow and content-backed.** Prefer a specific crate/attribute/
   command fragment over a generic word. Never match on "stellar" alone. Only
   set a strong (`likely`) signal for evidence that could not plausibly appear in
   an unrelated project.
3. **Decide the confidence impact.** Almost every new *weak* signal belongs in
   the `possible` branch of `confidence`; reserve the `likely` branch for
   Soroban contract evidence. Update the table above.
4. **Cover both directions.** Add a positive test and a negative test that the
   same file shape without the signal stays `none`. Extend
   `tests/test_stellar_detection.py` (or `test_stellar_detection_extra.py`).
5. **Keep the docs and adapters in sync.** Update this document, and add the new
   evidence-carrying path to `detection_relevant_paths` if the detector now reads
   a new file type in repository contexts.

## Testing

- `tests/test_stellar_detection.py` — signal/confidence basics, plain-Rust
  exclusion, manifest parsing, metadata shape.
- `tests/test_stellar_detection_extra.py` — additional edge cases.
- `tests/test_project_import_detection.py` — detection on imported projects and
  the import response.

Run them with:

```bash
pytest tests/test_stellar_detection.py tests/test_stellar_detection_extra.py \
       tests/test_project_import_detection.py
```
