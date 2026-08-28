# AI Code Assistant

A developer-focused AI code intelligence platform with GitHub integration,
workspace-aware analysis, plugin extensibility, and Stellar/Soroban-aware
developer tooling. This project is built incrementally across phases:

- **Phase 1** — Application Foundation: Flask application factory,
  PostgreSQL-backed models, Dockerized deployment, CI pipelines, and a test
  suite.
- **Phase 2** — Authentication & User Management: registration, login,
  logout, password hashing, and account management.
- **Phase 3** — AI Core Features: chat interface with streaming responses,
  prompt library, AI code generation and analysis tools, file upload, and
  conversation management.
- **Phase 4** — GitHub Integration & Repository Intelligence: OAuth connection,
  repository browser, commit history, issues and pull requests with AI
  analysis, and encrypted token storage.
- **Phase 5** — AI Workspaces & Project Intelligence: workspace CRUD, project
  import (GitHub or archive upload), lazy file explorer, project-wide search,
  AI project chat and analyses, and a project health dashboard.
- **Phase 6** — Collaboration, AI Code Review & Quality Tooling: AI code
  review for pull requests and projects (quality, security, tests), structured
  findings with `[CONFIRMED]`/`[SUGGESTION]` labels, review history and
  configuration, a quality dashboard with metrics, and workspace member
  foundations.
- **Phase 7** — Team Collaboration: workspace member lifecycle, email
  invitations, roles and permissions, notifications, mentions, activity/audit
  history, collaboration UI, and permission-aware AI collaboration.
- **Phase 8** — Plugins & Extensions **(in progress)**: plugin manifest,
  registry, capability model, and event system, plus Stellar/Soroban developer
  tooling (network configuration, read-only Horizon **and Stellar RPC**
  services, contract/account inspection, evidence-based project detection, and
  Stellar-aware AI analysis).

> **Status:** Phases 1–7 implemented. Phase 8 foundation implemented; the
> plugin/Stellar surface area is intentionally small and the remaining work is
> tracked as contributor issues under the **Phase 8 - Plugins & Extensions**
> milestone.

## Table of contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
  - [Local development](#local-development)
  - [Running with Docker](#running-with-docker)
- [Testing](#testing)
- [Configuration](#configuration)
- [CI / CD](#ci--cd)
- [Roadmap](#roadmap)
- [License](#license)

## Documentation

- [Team collaboration guide](docs/team-collaboration.md) — feature guide,
  roles matrix, invitation flow, FAQ, and developer guide.
- [Collaboration API reference](docs/api-collaboration.md) — every Phase 7
  endpoint with method, path, role requirement, params, and examples.
- [Plugin system](docs/plugins.md) — manifest spec, registry, capabilities,
  events, and how to write a plugin.
- [Stellar / Soroban tooling](docs/stellar.md) — what is implemented, network
  configuration, detection, inspection, and the Stellar-aware AI analysis.
- [Soroban / Stellar RPC](docs/soroban.md) — the read-only RPC client, its
  methods, ledger-key encoding, and security model.
- [Architecture](docs/architecture.md) — how the application is layered and
  how the Stellar tooling fits in.
- [Security model](docs/security.md) — threat review and controls for the
  plugin and Stellar architecture.

## Features

- **Flask application factory** — testable, environment-driven configuration.
- **User authentication** — registration, login, logout, remember-me, session
  management, and CSRF protection via Flask-WTF.
- **Secure password storage** — salted hashes via Werkzeug (never plain text).
- **PostgreSQL-first persistence** — SQLAlchemy models with Flask-Migrate
  schema migrations.
- **Containerized** — multi-stage `Dockerfile`, `docker-compose` with a
  health-checked Postgres service, and a non-root runtime user.
- **CI pipeline** — lint (ruff), formatting (black), tests (pytest), and
  Docker image build on every push/PR.
- **Clean UI** — dark, developer-focused theme with responsive vanilla CSS and
  progressive-enhancement JavaScript.

### Phase 2 — Authentication & user management

- **Registration** — create an account with a username and email; validates
  username length, email format, password strength (minimum 8 characters),
  password confirmation, and uniqueness of both username and email.
- **Login & logout** — email/password authentication with a remember-me
  option, last-login timestamp tracking, and disabled-account detection.
- **Password security** — salted password hashes via Werkzeug
  (`generate_password_hash` / `check_password_hash`); plain text is never
  stored.
- **Session management** — Flask-Login sessions with `@login_required`
  protection on authenticated routes and a shared account page (`/auth/me`).
- **Safe redirects** — post-login redirects are validated against an
  open-redirect attack (only same-host URLs are allowed).
- **CSRF protection** — all state-changing forms are protected via Flask-WTF.

### Phase 3 — AI core features

- **AI chat interface** — per-user conversations, message history, live
  server-sent-event (SSE) streaming, typing indicator, and client-side
  Markdown rendering with code blocks.
- **Prompt management** — save, edit, delete, favorite, categorize, and search
  reusable prompt templates.
- **AI code generation** — generate code from natural language, plus code
  actions: explain, refactor, find bugs, optimize, add comments, write
  documentation, and draft commit messages.
- **File support** — upload source files and run AI analysis over their
  contents (multi-language, UTF-8 text).
- **Conversation management** — rename, pin, search, delete, and export
  conversations as JSON.
- **Provider abstraction** — a provider-agnostic LLM service layer with an
  offline **mock provider** (default) and an OpenAI-compatible client. Set
  `LLM_PROVIDER=openai` and `OPENAI_API_KEY` for real responses.

### Phase 4 — GitHub integration & repository intelligence

- **GitHub OAuth connection** — connect/disconnect a GitHub account through a
  browser OAuth flow (scoped to `read:user repo`), with a signed state
  parameter to prevent CSRF on the callback.
- **Encrypted token storage** — access tokens are encrypted at rest with
  Fernet (AES-128 + HMAC-SHA256) using a key derived from `SECRET_KEY`; the
  plaintext token is never persisted, logged, or sent to the frontend.
- **Repository browser** — list and search repositories, browse branches and
  files (directory listing and tree view), search file names, and view file
  contents.
- **Commit history** — per-repository and per-path commit lists with author,
  date, and per-commit file/patch views.
- **Issues** — open/closed/all issue lists (pull requests excluded), issue
  detail pages with labels and body, and one-click **AI issue analysis**
  (summary, problem identification, suggested implementation, acceptance
  criteria, difficulty estimate).
- **Pull requests** — PR lists and detail pages with changed files and inline
  diffs, plus **AI code review** that flags potential bugs while clearly
  labeling `[CONFIRMED]` defects versus `[SUGGESTION]` hypotheses.
- **AI repository analysis** — summarize a repository from its README and
  structure, and ask questions about individual files. Context sent to the
  model is bounded (`GITHUB_MAX_CONTEXT_CHARS`) so a request never uploads a
  whole repository.
- **API reliability** — a dedicated GitHub API client with request timeouts,
  typed error taxonomy (auth, permission, not-found, rate-limit, network),
  exponential backoff retries on transient failures, and rate-limit awareness.
- **Authorization** — all GitHub API calls are made on the user's behalf with
  their own token, so GitHub's own permission model decides which
  repositories are accessible; no secrets are ever exposed to the client.

### Phase 5 — AI workspaces & project intelligence

- **Workspaces** — per-user workspaces with create, rename, delete, and a
  dashboard listing projects with import status.
- **Project import** — import a codebase from a connected GitHub repository
  or an uploaded `.zip` / `.tar.gz` archive. GitHub imports walk the blob
  tree and fetch bounded file contents; archive extraction is done entirely
  in memory (nothing is written to disk).
- **Import security** — archive members with absolute paths, `..` traversal,
  or symlinks are rejected/skipped; archive size, expanded size, and
  file-count caps (zip-bomb protection); VCS/vendor directories and secret
  files (`.env`, `.pem`, `.key`, …) are skipped; binary and oversized files
  keep metadata but no searchable content.
- **Lazy file explorer** — tree and single-file APIs load directories on
  demand and reject traversal paths; the file viewer shows language, size,
  and content with binary/oversized markers.
- **Project-wide search** — bounded filename and content search with literal
  (escaped) matching, case toggle, and match snippets; binary files are
  excluded from content hits.
- **AI project chat** — ask questions about the project; context is *bounded*
  (keyword-scored paths, key files, content fallback within a fixed character
  budget — never a whole-project dump) and available over SSE streaming.
- **AI analyses** — architecture, bug review, refactoring, test coverage,
  documentation, and dependency analyses. Findings are labeled
  `[CONFIRMED]` (supported by the files) versus `[SUGGESTION]` (inference).
- **Dependency inventory** — real manifests are parsed (`requirements.txt`,
  `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Pipfile`,
  `Gemfile`, `composer.json`); nothing is fabricated, and unpinned/insecure
  claims are surfaced only as `[SUGGESTION]`s.
- **Prompt-injection resistance** — repository file contents are explicitly
  framed as untrusted DATA in the system prompt, so instructions embedded in
  imported files are not followed.
- **Health dashboard** — per-project stats: file/searchable/test/doc counts,
  languages, dependency count, manifests, and indexing duration.

### Phase 6 — Collaboration, AI code review & quality tooling

- **AI PR code review** — review an open pull request on a connected GitHub
  repository. Context is *bounded* (at most `REVIEW_MAX_FILES` changed files
  and `REVIEW_MAX_CONTEXT_CHARS` of diff text, with an optional language
  filter), and review runs never merge, close, approve, or otherwise modify
  the PR.
- **Structured findings** — every review returns findings with severity,
  category, confidence, and a file/line location (never raw repository
  content). Findings are labeled `[CONFIRMED]` (supported by the code) versus
  `[SUGGESTION]` (inference), and project reviews drop findings below the
  configured `REVIEW_SEVERITY_THRESHOLD`.
- **Project reviews** — run quality, security, and test-analysis reviews over
  an imported project; repository content is explicitly framed as untrusted
  data in the prompt to resist prompt injection.
- **Review history & configuration** — per-project review history with a
  config snapshot on each run, and per-project review configuration (kinds,
  threshold, languages, focus areas, bounds). All review routes are
  owner-scoped.
- **Quality dashboard** — aggregate metrics (review counts by status/source,
  findings by severity/category, addressed state, recent activity) computed
  strictly from stored review rows — never fabricated.
- **Workspace members** — `WorkspaceMember` model with
  owner/contributor/viewer roles and owner-scoped membership APIs (role
  assignment and removal); member-level access delegation is a planned
  follow-up.

### Phase 7 — Team collaboration

- **Member lifecycle** — add members by username, update roles, remove
  (soft-delete that preserves history), self-service leave, and atomic
  ownership transfer (previous owner becomes a contributor, projects move to
  the new owner).
- **Email invitations** — invite registered or unregistered users with
  one-time hashed tokens, a public landing page, accept/decline, TTL expiry,
  owner cancellation, and per-workspace invite toggles with a default member
  role.
- **Roles & permissions** — central capability matrix in
  `app/services/permissions.py`; non-members and unknown roles fail closed,
  and inaccessible resources return uniform `404`s (no existence oracle).
- **Activity feed & audit** — every team action is recorded in an append-only
  event log; members see a non-audit activity feed, the owner sees the full
  audit subset with metadata.
- **Notifications** — per-user inbox (invitations, `@`mentions, membership,
  role changes, AI events) with an unread badge, mark-read/read-all, and
  per-category preferences.
- **Project discussion** — threaded comments on projects with `@username`
  mentions that notify active members.
- **Permission-aware AI** — prompts carry an escaped, bounded team roster, and
  project content access fails closed before any context is assembled.

### Phase 8 — Plugins & extensions (in progress)

**Implemented foundation**

- **Plugin manifests** — validated `manifest.json` support (id, name, version,
  description, author, entry point, compatibility, capabilities, permissions,
  dependencies, configuration) with a JSON Schema in `plugins/plugin.schema.json`.
  Invalid ids, versions, entry points, unknown capabilities, and empty
  capability lists are rejected; duplicate plugin ids cannot be registered.
- **Plugin registry** — registration, directory discovery, lookup, enable and
  disable, with predictable behavior and unit tests.
- **Capability model** — `Capability` enum plus per-workspace `CapabilityGrant`
  rows; capabilities are explicit and never granted automatically; workspace
  roles map to the capabilities they may exercise.
- **Event system** — `EventDispatcher` with a supported event registry
  (`project.created`, `review.completed`, `workspace.member_added`,
  `github.connected`, `ai.analysis.completed`, `stellar.analysis.completed`,
  `stellar.network.detected`, …). Handler failures are isolated; routes emit
  events safely via `emit_event`.
- **Stellar network foundation** — network presets (mainnet/testnet/futurenet/
  local), environment-driven configuration (testnet by default), and a
  read-only `StellarService` (network info, address validation, bounded account,
  transaction, ledger, asset, and account-history lookup) with SSRF-bounded,
  size-capped, redirect-refusing requests.
- **Stellar RPC (Soroban) client** — a read-only JSON-RPC client
  (`app/services/soroban_rpc.py`) implementing the current Stellar RPC method
  set (`getHealth`, `getLatestLedger`, `getNetwork`, `getLedgerEntries`,
  `getTransaction(s)`, `getLedgers`, `getEvents`, `getFeeStats`, …). It never
  signs, simulates, or submits transactions. See [docs/soroban.md](docs/soroban.md).
- **strkey / LedgerKey encoding** — minimal, fixture-verified SEP-23 strkey
  checksum validation and `LedgerKey` encoders used for contract/account
  inspection.
- **Contract & account inspection** — `inspect_contract`, `inspect_account`,
  `inspect_ledger_entry`, and `network_status` (read-only, honest about raw
  XDR), exposed through the `/stellar` page/APIs and the `flask stellar …` CLI.
- **Stellar/Soroban project detection** — heuristic, evidence-based detection
  (Soroban crates, `#[contractimpl]`/`#[contract]` attributes, SDK
  dependencies, `stellar.toml`/`.soroban` configs, `contracts/` layout,
  Stellar/Soroban CLI tooling) with explicit `none`/`possible`/`likely`
  confidence and network hints. A plain Rust crate is never misclassified as
  Soroban. Detection metadata is attached to import responses.
- **Stellar-aware AI analysis** — `stellar` and `stellar_security` project
  analysis kinds that reuse the Phase 7 content-access gate (owner-only, fails
  closed), never fabricate Stellar claims for non-Stellar projects, and ground
  their analysis in the indexed contract files, manifests, and Stellar
  configuration — including an honest live-RPC availability note.
- **Event wiring** — the app already emits events for project import/delete,
  member add/remove, AI analysis completion, Stellar analysis completion,
  Stellar network detection, and GitHub connection.
- **Plugin management API + UI** — workspace-scoped plugin management
  (`app/plugins/`): list registered plugins, inspect metadata, install a
  trusted/local validated manifest (identity-bound, no code execution, no
  auto-grants), enable/disable a workspace installation, and explicitly
  grant/revoke capabilities restricted to manifest-declared capabilities.
  Members may view; the workspace owner manages (Phase 7 `manage_plugins`
  role). Backend is authoritative; the UI is never trusted for authorization.

**Planned contributor work (not yet implemented)**

- Plugin dependency resolution, version compatibility, and a plugin
  marketplace.
- Full XDR/`SCVal` decoding of contract data, XDR transaction inspection, a
  network switcher UI, account/contract browsing UIs, a mock Stellar network
  for tests, and Stellar project templates.
- Deeper Stellar-specific AI prompts and security findings storage.
- CLI commands for plugin workflows.

All of the above is tracked as open issues under the **Phase 8 - Plugins &
Extensions** milestone.

## Tech stack

| Layer        | Technology                                        |
| ------------ | ------------------------------------------------- |
| Backend      | Python 3.12, Flask 3                              |
| Database     | PostgreSQL 16 (SQLite fallback for local dev)     |
| Auth         | Flask-Login, Flask-WTF, Werkzeug hashing          |
| Migrations   | Flask-Migrate (Alembic)                           |
| AI providers | Provider-agnostic service layer (mock + OpenAI)   |
| GitHub       | GitHub REST API, OAuth web flow, Fernet (cryptography) |
| Stellar      | Horizon (read-only) + Stellar RPC (read-only) + strkey/LedgerKey encoders + Soroban/Rust project detection (Phase 8) |
| Frontend     | HTML, vanilla CSS, vanilla JavaScript (SSE)       |
| Infrastructure | Docker, Docker Compose, GitHub Actions          |
| Quality      | pytest, ruff, black                               |

## Project structure

```
.
├── .github/workflows/     # GitHub Actions CI pipelines
├── app/
│   ├── auth/              # Authentication blueprint (register/login/logout)
│   ├── chat/              # Chat blueprint (conversations, SSE streaming)
│   ├── collaboration/     # Collaboration blueprint (invitations, notifications, comments, activity/audit, settings) (Phase 7)
│   ├── github/            # GitHub blueprint (OAuth, repo browser, issues, PRs) (Phase 4)
│   ├── main/              # Public routes, landing page, health check
│   ├── models/            # SQLAlchemy models (User, GithubAccount, ...)
│   ├── prompts/           # Prompt library blueprint (CRUD, search, favorites)
│   ├── reviews/           # AI code review blueprint (Phase 6)
│   ├── services/          # Service layer (LLM providers, GitHub API, crypto, import/search/analysis/metrics, plugins/events/capabilities/stellar (Phase 8))
│   ├── static/            # CSS and JavaScript assets
│   ├── templates/         # Jinja2 templates (pages + error pages)
│   ├── tools/             # AI tools blueprint (generate, analyze, actions)
│   ├── workspaces/        # Workspaces & project intelligence blueprint (Phase 5)
│   ├── config.py          # Environment-based configuration
│   ├── extensions.py      # Shared Flask extension instances
│   └── __init__.py        # Application factory
├── migrations/            # Alembic migration scripts (generated)
├── plugins/               # Plugin manifest JSON Schema
├── scripts/               # Operational helper scripts
├── tests/                 # pytest suite
├── Dockerfile             # Multi-stage production image
├── docker-compose.yml     # web + postgres orchestration
├── pyproject.toml         # Tooling configuration (pytest, ruff, black)
└── requirements*.txt      # Python dependencies
```

## Getting started

### Prerequisites

- Python 3.12+
- PostgreSQL 16 (optional — SQLite is used by default in development)
- Docker + Docker Compose (optional, for containerized runs)
- Git

### Local development

```bash
# 1. Clone and enter the project
git clone https://github.com/your-org/ai-code-assistant.git
cd ai-code-assistant

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements-dev.txt

# 4. Configure environment
cp .env.example .env             # then edit values as needed

# 5. Create the database tables (uses SQLite by default)
flask --app wsgi init-db

# 6. Run the dev server
python run.py
```

Open <http://localhost:5000> in your browser.

### Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

- Web app: <http://localhost:5000>
- PostgreSQL: `localhost:5432` (user `aica`, db `aica`)

The `web` service applies pending migrations before starting, and exposes a
health check at `GET /health`.

### Setting up GitHub OAuth (Phase 4)

1. Create an OAuth App at <https://github.com/settings/applications/new>:
   - **Homepage URL:** `http://localhost:5000`
   - **Authorization callback URL:** `http://localhost:5000/github/callback`
2. Copy the **Client ID** and **Client Secret** into `.env`:
   ```bash
   GITHUB_CLIENT_ID=your_client_id
   GITHUB_CLIENT_SECRET=your_client_secret
   ```
3. Restart the app and open **GitHub** in the navigation bar to connect your
   account.

> Tokens are encrypted before storage and used only server-side; they are
> never exposed in the browser. To disconnect (and remove the stored token),
> use **Disconnect** on the GitHub dashboard.

## Testing

```bash
pytest --cov=app --cov-report=term-missing
```

Tests run against an in-memory SQLite database so the suite is fast and
self-contained. Set `TEST_DATABASE_URL` to a PostgreSQL URL to run the same
suite against Postgres.

## Configuration

All configuration is environment-driven (see `.env.example`):

| Variable             | Default      | Description                              |
| -------------------- | ------------ | ---------------------------------------- |
| `APP_ENV`            | `development`| `development`, `testing`, or `production`|
| `SECRET_KEY`         | dev-only     | Flask secret — **required in production** |
| `DATABASE_URL`       | SQLite file  | SQLAlchemy connection string             |
| `SESSION_LIFETIME`   | `43200`      | Session lifetime in seconds              |
| `LLM_PROVIDER`       | `mock`       | LLM backend: `mock` (offline) or `openai`|
| `OPENAI_API_KEY`     | unset        | API key for the OpenAI provider          |
| `OPENAI_BASE_URL`    | OpenAI       | Custom/compatible endpoint               |
| `OPENAI_MODEL`       | `gpt-4o-mini`| Model used by the OpenAI provider        |
| `MAX_CONTENT_LENGTH` | `16777216`   | Max uploaded file size in bytes          |
| `GITHUB_CLIENT_ID`   | unset        | GitHub OAuth app client ID               |
| `GITHUB_CLIENT_SECRET`| unset       | GitHub OAuth app client secret           |
| `GITHUB_REDIRECT_URI`| callback URL | Explicit callback URL (optional)         |
| `GITHUB_API_URL`     | `https://api.github.com` | GitHub REST API base URL      |
| `GITHUB_SCOPES`      | `read:user repo` | OAuth scopes requested on connect    |
| `GITHUB_REQUEST_TIMEOUT` | `30`     | GitHub API request timeout (seconds)     |
| `GITHUB_MAX_CONTEXT_CHARS` | `40000` | Max repo context sent to the LLM       |
| `PROJECT_MAX_ARCHIVE_BYTES` | `52428800` | Max uploaded project archive (50 MB) |
| `PROJECT_MAX_SIZE_BYTES` | `524288000` | Max expanded project size (500 MB)   |
| `PROJECT_MAX_FILE_COUNT` | `20000`   | Max files importable into one project  |
| `PROJECT_MAX_FILE_CHARS` | `200000`  | Max text content stored per file       |
| `PROJECT_MAX_CONTEXT_CHARS` | `40000` | Max project context sent to the LLM  |
| `PROJECT_SEARCH_MAX_RESULTS` | `100` | Max results returned by one search query |
| `PROJECT_GITHUB_MAX_FILES` | `1000`   | Max file contents fetched per GitHub import |
| `PROJECT_SKIP_DIRS`    | `.git,node_modules,…` | Directory basenames skipped on import |
| `PROJECT_SKIP_SECRET_FILES` | `.env,.pem,…` | File names/prefixes skipped on import |
| `REVIEW_MAX_FILES`     | `40`        | Max changed files analyzed in one review |
| `REVIEW_MAX_CONTEXT_CHARS` | `40000` | Max diff/repo context sent to the LLM per review |
| `REVIEW_MAX_FINDINGS`  | `100`       | Max findings stored per review |
| `REVIEW_KINDS`         | `quality,security,tests` | Default project review kinds |
| `REVIEW_SEVERITY_THRESHOLD` | `low` | Min finding severity stored for project reviews |
| `INVITE_TTL_HOURS`     | `168`       | Invitation expiry window in hours (Phase 7) |
| `RATE_LIMIT_MAX`       | `30`        | Max attempts per IP/window for invitation endpoints (Phase 7) |
| `RATE_LIMIT_WINDOW_SECONDS` | `300`  | Rate-limit window in seconds (Phase 7) |
| `SMTP_HOST`           | unset       | SMTP host for invitation emails (Phase 7) |
| `SMTP_PORT`           | `587`       | SMTP port (Phase 7) |
| `SMTP_USER`           | unset       | SMTP username (Phase 7) |
| `SMTP_PASSWORD`       | unset       | SMTP password (Phase 7) |
| `MAIL_USE_TLS`        | `true`      | Use TLS for SMTP (Phase 7) |
| `MAIL_DEFAULT_SENDER` | unset       | From-address for outgoing email (Phase 7) |
| `PROJECT_MAX_MEMBER_CONTEXT` | `20`  | Max workspace members included in the AI team roster (Phase 7) |
| `STELLAR_NETWORK`   | `testnet`   | Stellar network: `mainnet`/`testnet`/`futurenet`/`custom` (Phase 8) |
| `STELLAR_HORIZON_URL`| unset      | Override Horizon endpoint (Phase 8)        |
| `STELLAR_RPC_URL`   | unset       | Override Soroban RPC endpoint (Phase 8)    |
| `STELLAR_REQUEST_TIMEOUT` | `15` | Outbound Stellar request timeout in seconds (Phase 8) |
| `STELLAR_MAX_RESPONSE_BYTES` | `2097152` | Cap on Stellar response body size (Phase 8) |
| `STELLAR_RPC_MAX_KEYS` | `100`   | Max ledger keys per RPC `getLedgerEntries` call (Phase 8) |
| `STELLAR_STRICT_HOST_VALIDATION` | `1` | DNS-verify public Stellar hosts resolve publicly (Phase 8) |

The production configuration fails fast at startup if `SECRET_KEY` or a
PostgreSQL `DATABASE_URL` is missing — it will never silently run with
insecure defaults.

> The app runs fully offline with the default **mock provider**: chat replies,
> code generation, and file analysis all work with canned responses. To enable
> real AI responses set `LLM_PROVIDER=openai` and `OPENAI_API_KEY` (or point
> `OPENAI_BASE_URL` at an OpenAI-compatible server).

## CI / CD

GitHub Actions runs on every push to `main` and on pull requests:

1. **Lint & format** — `ruff check` and `black --check`.
2. **Tests** — `pytest` with coverage reporting.
3. **Docker** — verifies the production image builds successfully.

## Roadmap

Phases are built incrementally and tracked as GitHub issues and milestones.

### Completed

- **Phase 1** — Application Foundation: Flask application factory,
  PostgreSQL-backed models, Dockerized deployment, CI pipelines, and a test
  suite. ✔
- **Phase 2** — Authentication & User Management: registration, login, logout,
  password hashing, and account management. ✔
- **Phase 3** — AI Core Features: chat interface with streaming responses,
  prompt library, AI code generation and analysis tools, file upload, and
  conversation management. ✔
- **Phase 4** — GitHub Integration & Repository Intelligence: OAuth connection,
  repository browser, commit history, issues and pull requests with AI
  analysis, and encrypted token storage. ✔
- **Phase 5** — AI Workspaces & Project Intelligence: workspace CRUD, project
  import (GitHub or archive upload), lazy file explorer, project-wide search,
  AI project chat and analyses, and a project health dashboard. ✔
- **Phase 6** — Collaboration, AI Code Review & Quality Tooling: AI code review
  for pull requests and projects, structured findings, review history and
  configuration, a quality dashboard, and workspace member foundations. ✔
- **Phase 7** — Team Collaboration: workspace member lifecycle, invitations,
  roles and permissions, notifications, mentions, activity/audit history,
  collaboration UI, and permission-aware AI collaboration. ✔

### In progress

- **Phase 8** — Plugins & Extensions: plugin manifests, registry, capability
  model, and event system, plus Stellar/Soroban developer tooling (read-only
  Horizon + Stellar RPC services, contract/account inspection, evidence-based
  project detection, and Stellar-aware AI analysis). Foundation implemented and
  tested; the remaining surface (full XDR/SCVal decoding, network switcher UI,
  account/contract browsing UIs, XDR inspection, mock network, deeper Stellar
  AI, plugin dependency resolution, CLI workflows) is tracked as open
  contributor issues under the Phase 8 milestone and is **not yet
  implemented**.

### Planned

- **Phase 9** — To be defined.
- **Phase 10** — To be defined.

## License

[MIT](./LICENSE)
