# Contributing to AI Code Assistant

Thanks for your interest in contributing! This project is built incrementally
across phases and is tracked as GitHub issues and milestones. This guide
describes how to set up the project, run checks, and open pull requests that
match the repository's conventions.

## Table of contents

- [Project overview](#project-overview)
- [Prerequisites](#prerequisites)
- [Local development setup](#local-development-setup)
- [Environment configuration](#environment-configuration)
- [Docker setup](#docker-setup)
- [Database setup and migrations](#database-setup-and-migrations)
- [Running the application](#running-the-application)
- [Chat feature developer guide](#chat-feature-developer-guide)
- [Running tests](#running-tests)
- [Running the JavaScript test runner](#running-the-javascript-test-runner)
- [Running ruff](#running-ruff)
- [Running black](#running-black)
- [Git workflow](#git-workflow)
- [Branch naming conventions](#branch-naming-conventions)
- [Commit conventions](#commit-conventions)
- [Choosing a GitHub issue](#choosing-a-github-issue)
- [Issue expectations](#issue-expectations)
- [Pull request requirements](#pull-request-requirements)
- [Testing requirements](#testing-requirements)
- [Stellar / Soroban contributions](#stellar--soroban-contributions)
- [Security reporting](#security-reporting)
- [Code review expectations](#code-review-expectations)
- [Code of conduct](#code-of-conduct)

## Project overview

AI Code Assistant is a production-grade AI coding assistant web application
built with Flask 3, PostgreSQL 16, Docker, and vanilla JavaScript. It provides:

- **Authentication & user management** — registration, login, password hashing.
- **AI core features** — streaming chat, a prompt library, code generation and
  analysis tools, file upload, and conversation management.
- **GitHub integration** — OAuth connection, repository browser, commit
  history, issues and pull requests with AI analysis.
- **Workspaces & project intelligence** — project import (GitHub or archive),
  lazy file explorer, project search, AI project chat and analyses, and a
  health dashboard.
- **AI code review & quality tooling** — pull request and project reviews
  (quality, security, tests) with structured findings, review history and
  configuration, a quality dashboard, and workspace member foundations.

See [README.md](./README.md) for the full feature documentation.

## Prerequisites

- **Python 3.12 or newer** — required by the project (`requires-python >=3.12`).
- **PostgreSQL 16** — optional for local development (SQLite is used by
  default); required for production.
- **Docker + Docker Compose** — optional, for the containerized run.
- **Git**.

## Local development setup

```bash
# 1. Clone and enter the project
git clone https://github.com/Ayinkx/ai-code-assistant.git
cd ai-code-assistant

# 2. Create and activate a virtual environment
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 3. Install dependencies (runtime + development tools)
pip install -r requirements-dev.txt

# 4. Configure environment
cp .env.example .env        # then edit values as needed
```

## Environment configuration

Configuration is environment-driven. Copy `.env.example` to `.env` and adjust
values. The application loads the `.env` file automatically at startup
(`python-dotenv`).

Important settings:

- `APP_ENV` — `development`, `testing`, or `production` (default `development`).
- `SECRET_KEY` — Flask secret; **required in production**.
- `DATABASE_URL` — SQLAlchemy connection string; defaults to a local SQLite
  file (`instance/app.db`) for development.
- `LLM_PROVIDER` — `mock` (offline, default) or `openai`; set `OPENAI_API_KEY`
  to enable real AI responses.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — needed for the GitHub OAuth
  flow (Phase 4). Create an OAuth App at
  <https://github.com/settings/applications/new> with the callback URL
  `http://localhost:5000/github/callback`.
- Phase 5/6 limits (`PROJECT_*`, `REVIEW_*`) control import/review bounds.

Never commit real secrets. `.env.example` contains only placeholders, and the
production configuration fails fast at startup if `SECRET_KEY` or a PostgreSQL
`DATABASE_URL` is missing.

## Docker setup

```bash
cp .env.example .env
docker compose up --build
```

- Web app: <http://localhost:5000>
- PostgreSQL: `localhost:5432` (user `aica`, db `aica`)

The `web` service applies pending migrations (`flask db upgrade`) before
starting gunicorn, and exposes a health check at `GET /health`.

## Database setup and migrations

The default development database is a local SQLite file, so no server is
needed to get started.

Bootstrap a fresh development database (creates all tables from the models):

```bash
flask --app wsgi init-db
```

Schema changes are managed with **Flask-Migrate** (Alembic). Never hand-edit
the database; generate a migration and commit it with your change:

```bash
# After changing a model, generate a migration
flask --app wsgi db migrate -m "describe the schema change"

# Review the generated file under migrations/versions/, then apply it
flask --app wsgi db upgrade
```

Migration scripts live in `migrations/versions/` and are reviewed just like
application code. Existing migrations must keep working for anyone on the
latest `main`.

## Running the application

Start the development server (host `0.0.0.0`, port `5000`, debug on):

```bash
python run.py
```

Open <http://localhost:5000> in your browser.

## Chat feature developer guide

The chat feature is the core of Phase 2. It is built around an LLM abstraction
layer so the rest of the application never talks to a provider SDK directly.

### How it fits together

| Layer             | Location                          | Responsibility                                                  |
| ----------------- | -------------------------------- | ------------------------------------------------------------------------ |
| Provider abstraction | `app/services/llm/base.py`           | `LLMProvider` protocol + `LLMMessage`/`LLMChunk` dataclasses.           |
| Mock provider     | `app/services/llm/mock.py`           | Offline deterministic streaming for tests and dev.                   |
| OpenAI provider   | `app/services/llm/openai.py`          | Streams chat completions from the OpenAI API.                       |
| Provider registry | `app/services/llm/__init__.py`       | `_PROVIDERS` map + `get_provider()` factory.                         |
| Chat service      | `app/services/chat.py`              | Orchestrates provider calls, persists messages, yields SSE events.  |
| Chat API          | `app/api/chat.py`                  | Flask blueprint exposing `/api/chat`.                             |
| Chat UI           | `app/static/js/chat.js`           | Fetch + `EventSource` consumer of the SSE stream.                   |

### Enabling a provider

The default provider is `mock`, which requires no network access and produces
deterministic output. To use a real provider:

1. Copy the environment template if you have not already:

   ```bash
   cp .env.example .env
   ```

2. Set the provider name and its credentials in `.env`:

   ```dotenv
   LLM_PROVIDER=openai
   OPENAI_API_KEY=sk-...
   LLM_MODEL=gpt-4o-mini
   ```

   `LLM_MODEL` is optional; when unset the provider uses its default model.

3. Restart the development server. The provider is resolved at request time
   through `get_provider()`, so no code change is needed.

4. Verify the active provider with `GET /api/chat/provider`.

If `LLM_PROVIDER` is unknown or the credentials are missing, the factory
raises a configuration error and the endpoint returns a `400` json error rather
than failing silently.

### API reference

All endpoints require an authenticated session and are prefixed with
`/api/chat`. Requests and responses are JSON unless noted otherwise.

#### `GET /api/chat/provider`

Returns the active provider and model.

```json
{ "provider": "mock", "model": "mock-1" }
```

#### `GET /api/chat/conversations`

Lists the current user's conversations, most recent first.

```json
[
  {
    "id": 12,
    "title": "Refactor auth middleware",
    "created_at": "2024-05-01T10:00:00Z",
    "updated_at": "2024-05-01T10:05:00Z"
  }
]
```

#### `POST /api/chat/conversations`

Creates a conversation.

Request:

```json
{ "title": "Refactor auth middleware" }
```

Response (`201`):

```json
{
  "id": 12,
  "title": "Refactor auth middleware",
  "created_at": "2024-05-01T10:00:00Z",
  "updated_at": "2024-05-01T10:00:00Z"
}
```

#### `GET /api/chat/conversations/<id>`

Returns a single conversation with its messages in order.

```json
{
  "id": 12,
  "title": "Refactor auth middleware",
  "messages": [
    { "id": 1, "role": "user", "content": "Explain this function.", "created_at": "2024-05-01T10:00:00Z" },
    { "id": 2, "role": "assistant", "content": "It validates the token...", "created_at": "2024-05-01T10:00:05Z" }
  ]
}
```

#### `DELETE /api/chat/conversations/<id>`

Deletes a conversation and its messages. Responds `204` with no body.

#### `POST /api/chat/conversations/<id>/messages`

Sends a user message and streams the assistant reply as SSE.

Request:

```json
{ "message": "Explain this function." }
```

Response: `text/event-stream` (SSE). See the event schema below.

#### SSE event schema

Every frame is a named event with a JSON `data` payload. The stream always
ends with either `done` or `error`.

| Event     | Payload fields                                                              |
| ---------- | ------------------------------------------------------------------------------ |
| `start`    | `{ "message_id": 3, "conversation_id": 12 }` — the persisted user message.            |
| `delta`    | `{ "content": "token chunk" }` — incremental assistant text.                    |
| `done`     | `{ "message_id": 4, "content": "full reply" }` — final persisted reply.     |
| `error`    | `{ "code": "provider_error", "message": "..." }` — stream failure.         |

Sample stream:

```text
event: start
data: {"message_id": 3, "conversation_id": 12}

event: delta
data: {"content": "This function "}

event: delta
data: {"content": "validates the token."}

event: done
data: {"message_id": 4, "content": "This function validates the token."}

```

### Adding a new LLM provider

Providers are pluggable. To add one:

1. Create `app/services/llm/<name>.py` and subclass or implement the
   `LLMProvider` protocol from `app/services/llm/base.py`. The protocol requires:

   - `name` -- a stable identifier used in config and responses.
   - `model` -- the default model name.
   - `stream(messages, **kwargs)` -- a generator yielding `LLMChunk` objects.

2. Register it in `app/services/llm/__init__.py` by adding an entry to the
   `_PROVIDERS` map:

   ```python
   _PROVIDERS = {
       "mock": MockProvider,
       "openai": OpenAIProvider,
       "<name>": MyProvider,
   }
   ```

3. Add any credentials to `.env.example` and document them here and in
   the README. Read them in your provider's constructor via `app.config`.

4. Add tests under `tests/services/llm/` covering the stream generator
   with a mocked HTTP client. Do not hit the network in tests.

5. Update the `GET /api/chat/provider` documentation and any provider
   list in the README.

## Running tests

The suite runs against an in-memory SQLite database, so it is fast and
self-contained. Tests live in `tests/`, with shared fixtures in
`tests/conftest.py` (`app`, `client`, `db`, `make_user`, `login`).

Run the full suite with coverage (this matches CI):

```bash
pytest --cov=app --cov-report=term-missing
```

Notes:

- `filterwarnings = ["error::DeprecationWarning"]` is configured, so your code
  must not trigger deprecated-API warnings.
- Set `TEST_DATABASE_URL` to a PostgreSQL URL to run the same suite against
  Postgres instead of the in-memory SQLite default.
- `APP_ENV=testing` is set by CI; the testing config disables CSRF and uses an
  in-memory database.

## Running the JavaScript test runner

The frontend is vanilla JavaScript with no bundler. Unit tests for the
browser modules live in `tests/js`/` and run on Node with the built-in
test runner:

```bash
# Run all JavaScript tests
node --test tests/js

# Run a single file
node --test tests/js/chat.test.js

```

The runner executes any `*.test.js` file and reports TAP (or spec) output.
Keep modules pure and export the functions you want to test so they can be
imported without a DOM. The JavaScript suite is run in CI alongside the Python
suite.

## Running ruff

Ruff is the linter. Run it exactly as CI does:

```bash
ruff check .
```

Ruff is configured in `pyproject.toml` (line length 100, target Python 3.12,
`migrations/` excluded, rules `E F W I UP B C4 SIM RUF`). Fix issues with:

```bash
ruff check . --fix
```

## Running black

Black is the formatter. Verify formatting exactly as CI does:

```bash
black --check .
```

Black is configured in `pyproject.toml` (line length 100, `migrations/`
excluded). Format in place with:

```bash
black .
```

## Git workflow

- The default branch is `main`. All contributions land via **pull requests
  targeting `main`** — never push to `main` directly.
- GitHub Actions runs on every push to `main` and every pull request:
  1. **Lint & format** — `ruff check .` and `black --check .`.
  2. **Tests** — `pytest --cov=app --cov-report=term-missing`.
  3. **Docker** — verifies the production image builds.
- Your PR must pass all three jobs before it can be merged.
- Keep your branch up to date with `main` before requesting review (rebase or
  merge `main` as appropriate).

## Branch naming conventions

There is no enforced convention, but follow this pattern so branches are easy
to identify:

```
<type>/<issue-number>-<short-slug>
```

Examples:

- `feat/123-add-oauth-refresh`
- `fix/45-handle-rate-limit`
- `docs/update-readme`

Use the same `type` prefixes as commit messages (see below). If the work has
no issue, use `noissue` in place of the issue number.

## Commit conventions

This repository uses **Conventional Commits**. Recent history:

- `feat(phase5): implement AI workspaces & project intelligence`
- `fix(ci): resolve test path and docker build failures`
- `docs: update roadmap through phase 6`

Rules:

- Format: `<type>(<optional scope>): <imperative summary>`.
- Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`,
  `build`, `ci`, `perf`, `security`.
- Use the scope to indicate an area, e.g. a phase number (`phase5`) or
  subsystem (`ci`).
- Write the summary in lowercase, imperative mood, and keep it under 50–70
  characters where possible.
- Reference the issue when relevant: `feat: add OAuth refresh (#123)`.
- Commit only the files that belong to the change; stage related changes
  together and avoid mixing unrelated edits in one commit.

## Choosing a GitHub issue

- **Pick an existing issue before starting substantial work.** Substantial
  changes without a linked issue may not match the project direction.
- **Look for the labels first**:
  - `good first issue` / `help wanted` — good starting points.
  - `difficulty/easy`, `difficulty/medium`, `difficulty/hard` — effort sizing.
  - `priority/low` … `priority/critical` — urgency/importance.
  - `phase-2` … `phase-6` — which milestone the work belongs to.
  - `backend`, `frontend`, `ai`, `github-integration`, `security`, `testing`,
    `quality`, `code-review`, `infrastructure`, `performance`,
    `documentation`, `bug`, `enhancement` — the area of work.
- **Avoid duplicate work.** Search open and closed issues (including recently
  closed PRs) before starting. Comment on the issue to let others know you are
  working on it.
- **Claim the issue** by leaving a comment (e.g. "I'll take this"). If you
  start work and cannot finish, say so so someone else can pick it up.
- Ask for clarification in the issue thread if the acceptance criteria are
  unclear rather than guessing.

## Issue expectations

- Describe **what** you want to change and **why**, with enough detail for
  someone to act without further questions.
- Include reproduction steps for bugs, and expected vs. actual behavior.
- Label the issue appropriately and attach it to the matching phase milestone
  if you know it.
- Keep scope realistic — prefer several focused issues over one sprawling one.
- If your issue is a feature idea, describe the problem it solves and a
  suggested approach.

## Pull request requirements

Before opening a PR, confirm that you:

- **Chose an existing issue** and linked it in the PR description (e.g.
  `Closes #123`) so the work is traceable.
- **Avoided duplicate work** by searching for related open PRs first.
- **Kept the PR focused** — one logical change per PR. Split unrelated
  changes into separate PRs; they are easier to review and merge.
- **Included tests where appropriate** and confirmed the whole suite passes
  locally.
- **Updated documentation when necessary** — README, `.env.example`, and this
  guide should reflect behavior changes (new features, new settings, new
  directories).
- Ran `ruff check .` and `black --check .` locally; CI must be green.
- Provided a clear PR description: what changed, why, how it was tested, and
  any screenshots/notes for review.

Keep PRs small and reviewable. Large PRs are harder to review and more likely
to be sent back for splitting.

## Testing requirements

- Add or update tests for every behavior change. The project has a mature
  pytest suite (`tests/`) with fixtures in `tests/conftest.py`.
- Follow existing test structure: route/API tests use the `client` fixture,
  service tests use the `app` fixture, and model tests assert relationships
  and constraints.
- Do not leave the suite red: run the full test suite before pushing.
- Do not add new Python or JavaScript dependencies unless truly necessary and
  agreed in the issue — the dependency set is intentionally small and pinned.

## Stellar / Soroban contributions

The project includes Stellar/Soroban developer tooling (see
[`docs/stellar.md`](docs/stellar.md) and [`docs/soroban.md`](docs/soroban.md)).
Stellar work is tracked under the **Phase 8** milestone with the
`stellar`/`soroban` labels and is easy to pick up.

### Where Stellar code lives

| Concern                         | Location                                          |
| ------------------------------- | ------------------------------------------------- |
| Horizon read service            | `app/services/stellar.py`                         |
| Stellar RPC (read-only) client  | `app/services/soroban_rpc.py`                     |
| strkey / LedgerKey encoders     | `app/services/stellar_xdr.py`                     |
| Account/contract inspection     | `app/services/stellar_inspection.py`              |
| Project detection               | `app/services/stellar_detection.py`               |
| Stellar AI analysis             | `app/services/project_analysis.py`                |
| Web page + read-only APIs       | `app/stellar/`                                    |
| CLI                              | `app/services/stellar_cli.py`                     |

The `flask stellar …` CLI commands support `--json` for scriptable output and
documented exit codes (0 success, 2 service error/not found, 3 invalid input);
see [docs/stellar.md](docs/stellar.md).

### Rules for Stellar contributions

- **Keep it read-only.** The project never signs, simulates, or submits
  transactions, and it does not handle keys. Features that move toward
  wallets/custody are out of scope by design.
- **Keep it SSRF-safe.** Endpoints come from configuration only. New network
  code must go through the existing bounded transport (`_rpc_call` /
  `_get_json`) with `allow_redirects=False`, the base-URL check, timeouts, and
  size caps intact. Never add user-supplied URLs.
- **Never fabricate.** Detection must be evidence-based with explicit
  confidence (`none`/`possible`/`likely`); a plain Rust crate is never
  classified as Soroban. AI analysis must never claim live ledger/contract
  data when the RPC is unavailable.
- **Be honest about XDR.** Return raw XDR bounded and marked *not decoded*;
  do not pretend to decode values you do not decode.
- **Verify encoders.** Any change to `stellar_xdr.py` must keep the fixture
  tests green (they pin exact bytes from the official Stellar docs).

### Developing against a Stellar network

- Defaults are **testnet** — safe for development.
- The network configuration and endpoint safety rules are described in
  [`docs/stellar.md`](docs/stellar.md#network-configuration) and
  [`docs/soroban.md`](docs/soroban.md#security-model). The short local-node
  walkthrough below uses the repository's documented mock, so it does not
  require a locally installed `stellar-core` or `stellar-rpc`.

#### Local development node walkthrough

1. In the activated virtual environment, start the deterministic read-only
  mock in one terminal and leave it running:

  ```bash
  python -m app.services.stellar_mock
  ```

  The process prints an ephemeral loopback port, for example:

  ```text
  Horizon: http://127.0.0.1:54321
  RPC:     http://127.0.0.1:54321/rpc
  ```

  The mock implements the Horizon-style and read-only RPC methods used by the
  app. It is the same implementation exercised by
  [`tests/test_stellar_mock_network.py`](tests/test_stellar_mock_network.py),
  and can be stopped with `Ctrl+C`.

2. In a second terminal, use the port printed by the mock. Configure the
  application explicitly as a custom loopback network. PowerShell:

  ```powershell
  $env:STELLAR_NETWORK = "custom"
  $env:STELLAR_HORIZON_URL = "http://127.0.0.1:54321"
  $env:STELLAR_RPC_URL = "http://127.0.0.1:54321/rpc"
  ```

  Bash or zsh:

  ```bash
  export STELLAR_NETWORK=custom
  export STELLAR_HORIZON_URL=http://127.0.0.1:54321
  export STELLAR_RPC_URL=http://127.0.0.1:54321/rpc
  ```

  Replace `54321` with the port printed in the first terminal. Keep the
  `/rpc` suffix on `STELLAR_RPC_URL`; the Horizon URL is the base URL.

3. Verify the live RPC path through the same command contributors use for
  troubleshooting:

  ```bash
  flask --app wsgi stellar health
  ```

  A healthy mock prints `status: healthy`, a latest ledger, a retention
  window, and a protocol version. Use `--json` for a scriptable check. A
  service error or unreachable node exits with code `2`; invalid input exits
  with code `3` as documented in [`docs/stellar.md`](docs/stellar.md#cli).

For a real local `stellar-core`/`stellar-rpc`, use the same three settings and
point both URLs at that node's loopback listeners. The app accepts custom
endpoints only on loopback hosts; public-network endpoints require HTTPS and
the clients refuse redirects, enforce timeouts, and cap response sizes. Never
put a user- or project-supplied URL into this configuration. See
[`docs/soroban.md`](docs/soroban.md#network-configuration) for the full
configuration model and [`app/services/stellar_mock.py`](app/services/stellar_mock.py)
for the mock's supported fixture surface.

#### Extending a Stellar feature end to end

Use a small read-only inspection method as the model. For example, adding a
new inspection of a bounded RPC value should follow this sequence:

1. **Service method:** add the developer-facing function in
  `app/services/stellar_inspection.py`, and use the existing
  `SorobanRpcClient`/`StellarService` instead of making a new HTTP call. Keep
  address or key validation, result bounds, and explicit “not found” or
  “undecodable” results consistent with the neighboring inspection methods.
  The transport rules are centralized in `app/services/soroban_rpc.py` and
  `app/services/stellar.py`; do not accept a URL, sign, simulate, or submit a
  transaction.
2. **Test the service first:** add deterministic assertions beside the related
  tests in `tests/test_stellar_inspection.py` or
  `tests/test_stellar_mock_network.py`. Extend the mock fixture only with
  bounded, read-only data when the method needs a new response shape. Add
  invalid-input, unavailable-node, and malformed/unsupported-data coverage
  where applicable. Do not call a public Stellar network from tests.
3. **Expose it through the CLI:** add a command in
  `app/services/stellar_cli.py` using the existing exit-code and `--json`
  conventions. Add success and error cases to `tests/test_stellar_cli.py`.
  Reuse the service method so CLI and web results cannot drift.
4. **Expose it through the API/UI when it is user-facing:** add the
  authenticated read-only route in `app/stellar/routes.py`, wire the control
  into `app/static/js/stellar.js` and its template if needed, and add route
  coverage in `tests/test_stellar_routes.py`. For project-specific behavior,
  use the owner-scoped workspace route and existing analysis gates rather than
  creating a parallel authorization path.
5. **Update the docs:** record the method and its honest limits in
  [`docs/stellar.md`](docs/stellar.md), add RPC/security detail to
  [`docs/soroban.md`](docs/soroban.md) only when the RPC contract changed, and
  update the command/API list here. Link to the implementation and tests so
  the next contributor can follow the same path.

For a **new detection signal**, the analogous path is
`detect_stellar_project()` in `app/services/stellar_detection.py` -> focused
cases in `tests/test_stellar_detection.py` (and the `_extra` or import tests
when relevant) -> `project_stellar_metadata()` and the existing import,
workspace API, project panel, and analysis consumers -> the detection section
of [`docs/stellar.md`](docs/stellar.md#project-detection). Detection must be
evidence-based with `none`/`possible`/`likely` confidence; plain Rust and
keyword-only prose must remain non-Stellar.

Before opening a PR, run the focused Stellar command list below and then the
full suite. Keep the SSRF, read-only, bounded-response, owner-authorization,
and no-fabrication rules intact; these are security invariants, not optional
implementation details.

### Testing Stellar work

```bash
pytest tests/test_soroban_rpc.py tests/test_stellar_xdr.py \
       tests/test_stellar_xdr_decode.py tests/test_stellar_xdr_transaction.py \
       tests/test_stellar_inspection.py tests/test_stellar_security.py \
       tests/test_stellar_detection.py tests/test_stellar_detection_extra.py \
       tests/test_stellar_analysis.py tests/test_stellar_routes.py \
       tests/test_stellar_cli.py tests/test_stellar_network_switcher.py \
       tests/test_stellar_mock_network.py \
       tests/test_stellar_security_findings.py \
       tests/test_soroban_scaffold.py tests/test_project_import_detection.py
```

## Plugin contributions

Plugin work is tracked under the **Phase 8 - Plugins & Extensions** milestone
(`plugin`/`phase-8` labels). See [`docs/plugins.md`](docs/plugins.md) for the
manifest schema, capability model, event dispatch/enforcement, lifecycle hooks,
configuration, audit/error reporting, and the plugin-writing guide.

### Where plugin code lives

| Concern                              | Location                                      |
| ------------------------------------ | --------------------------------------------- |
| Manifest parsing/validation + registry | `app/services/plugins.py`                   |
| Capabilities + grants                | `app/services/capabilities.py`               |
| Event dispatch + capability checks   | `app/services/events.py`                     |
| Audit trail + error reports          | `app/services/plugin_audit.py`, `app/services/plugin_errors.py` |
| Compatibility / config / ops logic   | `app/services/plugin_{compat,config,ops}.py` |
| Operator CLI                         | `app/services/plugins_cli.py`                |
| Management API + UI                  | `app/plugins/`                                |
| Database models                      | `app/models/plugin*.py`                       |

### Rules for plugin contributions

- **Never grant implicitly.** Capabilities are explicit, per-workspace grants;
  install/enable never creates them.
- **Keep installs local/trusted.** Do not add remote/URL installation. Never
  make the management API/CLI load or execute plugin code.
- **Fail closed.** Dispatch-time capability enforcement and workspace
  isolation must stay intact (see `docs/security.md`).
- **Isolate failures.** Plugin handler/hook failures must never crash a request
  or corrupt the registry; keep them recorded (`plugin_errors.py`), not fatal.
- **Protect secrets/config.** Workspace plugin config may hold secrets — keep
  it owner-only and omitted from list/inspect surfaces.

### Testing plugin work

```bash
pytest tests/test_plugins_manifest.py tests/test_capabilities.py \
       tests/test_plugins_api.py tests/test_plugin_audit.py \
       tests/test_event_authorization.py tests/test_event_wiring.py \
       tests/test_plugin_integration.py tests/test_plugin_lifecycle.py \
       tests/test_plugin_config.py tests/test_plugin_compat.py \
       tests/test_plugin_error_reports.py tests/test_plugins_cli.py
```

## Security reporting

See [SECURITY.md](SECURITY.md) for how to report a vulnerability privately.

Guidelines for security-sensitive work:

- Do not post secrets, credentials, or working exploit code in public issues
  or commit them to the repository.
- Never commit real secrets: `.env`, API keys, OAuth client secrets, or
  database passwords. `.env` and `.env.example` values are placeholders only.
- When handling untrusted input (imported projects, uploaded files, review
  context), preserve the existing safety behavior: path traversal checks,
  size/file-count caps, secret-file skipping, and treating repository content
  as untrusted data in prompts.

## Code review expectations

- Reviews are expected on every PR; reviewers should be constructive and
  specific.
- A reviewer should check: correctness, test coverage, documentation updates,
  security of untrusted input handling, and compliance with ruff/black.
- As the author, respond to review comments, request re-review once addressed,
  and avoid merging until all CI jobs are green.
- AI-generated findings in this project are labeled `[CONFIRMED]` (supported
  by the code) vs `[SUGGESTION]` (inference) — treat them accordingly and
  never treat generated output as ground truth.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). We expect everyone to treat each
other professionally and with respect: be welcoming to new contributors, give
and receive feedback constructively, and assume good intent. Harassment or
abusive behavior is not acceptable in issues, PRs, or reviews.
