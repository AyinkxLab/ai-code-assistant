# A Code Assistant

A developer-focused AI code intelligence platform with GitHub integration,
workspace-aware analysis, plugin extensibility, and Stellar/Soroban-aware
developer tooling. This project is built incrementally across phases:

- Phase 1 — Application Foundation: Flask application factory,
  PostgreSQL-backed models, Dockerized deployment, CI pipelines, and a test
  suite.
- Phase 2 — Authentication & User Management: registration, login,
  logout, password hashing, and account management.
- Phase 3 — AI Core Features: chat interface with streaming responses,
  prompt library, AI code generation and analysis tools, file upload, and
  conversation management.
- Phase 4 — GitHub Integration & Repository Intelligence: OAuth connection,
  repository browser, commit history, issues and pull requests with AI
  analysis, and encrypted token storage.
- Phase 5 — AI Workspaces & Project Intelligence: workspace CRUD, project
  import (GitHub or archive upload), lazy file explorer, project-wide search,
  AI project chat and analyses, and a project health dashboard.
- Phase 6 — Collaboration, AI Code Review & Quality Tooling: AI code
  review for pull requests and projects (quality, security, tests), structured
  findings with `[CONFIRMED]`/`[SUGGESTION]` labels, review history and
  configuration, a quality dashboard with metrics, and workspace member
  foundations.
- Phase 7 — Team Collaboration: workspace member lifecycle, email
  invitations, roles and permissions, notifications, mentions, activity/audit
  history, collaboration UI, and permission-aware AI collaboration.
- Phase 8 — Plugins & Extensions **(in progress)**: plugin manifest,
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
- [Chat feature](#chat-feature)
- [Testing](#testing)
- [Configuration](#configuration)
- [CI / CD](#ci--cd)
- [Roadmap](#roadmap)
- [License](#license)

## Documentation

- [Chat feature guide](docs/chat.md) — provider setup, using the chat,
  the SSE event schema, and a complete chat API reference.
- [Adding a new LLM provider](docs/chat.md#adding-a-new-llm-provider) — a step-by-step
  guide tied to the provider abstraction layer.
- [Developer workflow](docs/developer-workflow.md) — migrations, tests, and the JS test
  runner.
- [Reviews & quality tooling](docs/reviews.md) — the Reviews pages, the
  `REVIEW_` settings, the finding vocabulary, and the review API.
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
- [Soroban workflow guide](docs/soroban-workflow.md) — end-to-end walkthrough:
  import a Soroban repo, see detection, run analysis, and scaffold a contract.
- [Architecture](docs/architecture.md) — how the application is layered and
  how the Stellar tooling fits in.
- [Security model](docs/security.md) — threat review and controls for the
  plugin and Stellar architecture.
- [Security policy](SECURITY.md) — how to report a vulnerability.
- [Code of conduct](CODE_OF_CONDUCT.md) — community standards.

## Features

- **Flask application factory** — testable, environment-driven configuration.
- \*\*User authentication\*\* — registration, login, logout, remember-me, session
  management, and CSRF protection via Flask-WTF.
- **Secure password storage** — salted hashes via Werkzeug (never plain text).
- **PostgreSQL-first persistence** — server-side sessions and persistent
  conversation/message storage.
- \*\*Containerized\*\* — multi-stage `Dockerfile`, `docker-compose` with a
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
- \*\*Session management\*\* — Flask-Login sessions with `@login_required`
  protection on authenticated routes and a shared account page (`/auth/me`).
- **Safe redirects** — post-login redirects are validated against an
  open-redirect attack (only same-host URLs are allowed).
- **CSRF protection** — all state-changing forms are protected via Flask-WTF.
- \*\*Encrypted provider keys\*\* — bring your own LLM provider API keys
  (`/keys/`); they are encrypted at rest with AES-256-GCM (random nonce per
  record), only ever shown redacted, and decrypted in memory only for a
  provider call.

### Phase 3 — AI core features

- **AI chat interface** — per-user conversations, message history, live
  server-sent-event (SSE) streaming, typing indicator, and client-side
  Markdown rendering with code blocks.
- \*\*Prompt management\*\* — save, edit, delete, favorite, categorize, and search
  reusable prompt templates, with version history (a version is recorded on
  every save), a diff timeline, and revert-to-version.
- \*\*AI code generation\*\* — generate code from natural language, plus code
  actions: explain, refactor, find bugs, optimize, add comments, write
  documentation, and draft commit messages.
- \*\*File support\*\* — upload source files and run AI analysis over their
  contents (multi-language, UTF-8 text).
- \*\Conversation management\*\* — rename, pin, search, delete, and export
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
- \*\*Commit history\*\* — per-repository and per-path commit lists with author,
  date, and per-commit file/patch views.
- **Issues** — open/closed/all issue lists (pull requests excluded), issue
  detail pages with labels and body, and one-click **AI issue analysis**
  (summary, problem identification, suggested implementation, acceptance
  criteria, difficulty estimate).
- \*\*Pull requests\*\* — PR lists and detail pages with changed files and inline
  diffs, plus **AI code review** that flags potential bugs while clearly
  labeling `[CONFIRMED]` defects versus `[SUGGESTION]` hypotheses.
- **AI repository analysis** — summarize a repository from its README and
  structure, and ask questions about individual files. Context sent to the
  model is bounded (`GITHUB_MAX_CONTEXT_CHARS`) so a request never uploads a
  whole repository.
- **API reliability** — a dedicated GitHub API client with request timeouts,
  typed error taxonomy (auth, permission, not-found, rate-limit, network),
  exponential backoff retries on transient failures, and rate-limit awareness.
- \*\*Authorization\*\* — all GitHub API calls are made on the user's behalf with
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
- \*\*Dependency inventory\*\* — real manifests are parsed (`requirements.txt`,
  `package.json`, `pyproject.toml`, `CargoToml`, `go.mod`, `Pipfile`,
  `Gemfile`, `composer.json`); nothing is fabricated, and unpinned/insecure
  claims are surfaced only as `[SUGGESTION]`s.
- **Prompt-injection resistance** — repository file contents are explicitly
  framed as untrusted DATA in the system prompt, so instructions embedded in
  imported files are not followed.
- \*\*Health dashboard\*\* — per-project stats: file/searchable/test/doc counts,
  languages, dependency count, manifests, and indexing duration.

### Phase 6 — Collaboration, AI code review & quality tooling

- **AI PR code review** — review an open pull request on a connected GitHub
  repository. Context is *bounded* (at most `REVIEW_MAX_FILES` changed files
  and `REVIEW_MAX_CONTEXT_CHARS` of diff text, with an optional language
  filter), and review runs never merge, close, approve, or otherwise modify
  the PR.
- \*\*Structured findings\*\* — every review returns findings with severity,
  category, confidence, and a file/line location (never raw repository
  content). Findings are labeled `[CONFIRMED]` (supported by the code) versus
  `[SUGGESTION]` (inference), and project reviews drop findings below the
  configured `REVIEW_SEVERITY_THRESHOLD`.
- **Project reviews** — run quality (`analyze_code_quality`: readability,
  maintainability, duplication, dead code), security, and test-analysis
  (`analyze_tests`: coverage gaps, missing assertions, flaky tests) reviews over
  an imported project; repository content is explicitly framed as untrusted
  data.

## Chat feature

The chat feature is the core of Phase 3. It lets a signed-in user create
conversations, send messages, and receive model responses streamed over
Server-Sent Events (SSE). The full guide — provider configuration, the
complete API reference, the SSE event schema, and how to add a new LLM
provider — lives in [`docs/chat.md`](docs/chat.md).

### Enabling a provider

By default the app runs the offline **MockProvider**, which returns
deterministic canned responses and needs no network access. To use a real
OpenAI-compatible model, set the following environment variables before
starting the app:

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini  # optional, defaults to gpt-4o-mini
export OPENAI_BASE_URL=https://api.openai.com/v1  # optional
export LLM_MAX_TOKENS=2048                     # optional
```

Alternatively, each user can store their own provider key at `/keys/`.
Those keys are encrypted at rest (AES-256-GCM) and are only decrypted in
memory for the duration of a provider call. See [`docs/chat.md`](docs/chat.md)
for the full precedence rules (environment variable vs. per-user key).

#### Using the chat

1. Sign in and open `/chat/`.
2. Click *New conversation*.
3. Type a message and submit. The client opens an SSE connection to
   `/chat/api/conversations/<id>/messages` and renders tokens as they
   arrive.
4. Use the sidebar to rename, pin, search, export, and delete
   conversations.

The exact request/stream shapes are documented in the
[chat API reference](docs/chat.md#api-reference).

### Adding a new LLM provider

Providers implement the abstraction layer in `app/services/llm/`. The
step-by-step guide (file layout, required methods, registration, and
testing) is maintained in
[`docs/chat.md`](docs/chat.md#adding-a-new-llm-provider).

### Developer workflow

Running migrations, the Python test suite, and the JavaScript test
runner is documented in [`docs/developer-workflow.md`](docs/developer-workflow.md).

## Tech stack

- **Python 3.12+** + **Flask** (application factory)
- **SQAlchemy** + **Flask-Migrate** for PostgreSQL persistence
- **Flask-Login** + **Flask-WTF** for authentication and CSRF
- **pytest** for the Python test suite
- **Node.js** + **Jest** for the frontend JavaScript test runner
- **Docker** + **docker-compose** for local development and deployment
- **ruff** + **black** for linting and formatting

## Project structure

```
app/
  auth/            # registration, login, logout, account management
  chat/            # conversations, messages, SSE endpoints
  keys/            # encrypted provider API keys
  services/
    llm/            # provider abstraction layer (MockProvider, OpenAIProvider)
  models
  templates
  static/
    js/             # frontend JavaScript + Jest tests
docs/
  chat.md          # chat feature guide + API reference
  developer-workflow.md  # developer workflow guide
migrations/        # Flask-Migrate revisions
tests/             # pytest suite
```

## Getting started

### Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask db upgrade
flask run
```

See [`docs/developer-workflow.md`](docs/developer-workflow.md) for the
complete developer workflow, including migrations and the JS / Python test
runners.

### Running with Docker

```bash
docker compose up --build
```

The app is then available at `http://localhost:5000/`.

## Testing

```bash
pytest                        # Python test suite
npm test                      # JavaScript test runner (Jest)
```

See [`docs/developer-workflow.md`](docs/developer-workflow.md) for details on
running a single test file, watch mode, and CI parity.

## Configuration

Key environment variables:

| Variable | Description |
| --- | --- |
| `SCRETIT_KEY` in Required in production. Signs sessions and derives the GitHub token encryption key. |
| `DATABASE_URL` | PostgreSQL connection URL. |
| `LLM_PROVIDER` | `mock` (default) or `openai`. |
| `OPENAI_API_KEY` | API key for the OpenAI-compatible provider. |
| `OPENAI_MODEL` | Model name (defaults to `gpt-4o-mini`). |
| `OPENAI_BASE_URL` | Override the API base URL (for proxies or compatible endpoints). |
| `LLM_MAX_TOKENS` | Maximum tokens per response. |

See [`docs/chat.md`](docs/chat.md) for the complete chat-related setting list.

## CI / CD

Every push and pull request runs:

1. `ruff check .`
2. `black --check .`
3. `pytest`
4. `npm test`
5. `docker build .`

## Roadmap

- [x] **Phase 1** — Application foundation.
- [x] **Phase 2** — Authentication & user management.
- [x] **Phase 3** — AI core features (chat, prompts, code tools, files).
- [x] **Phase 4** — GitHub integration & repository intelligence.
- [x] **Phase 5** — AI workspaces & project intelligence.
- [x] **Phase 6** — Collaboration, AI code review & quality tooling.
- [x] **Phase 7** — Team collaboration.
- [ ] **Phase 8** — Plugins & extensions (in progress).

## License

This project is licensed under the terms of the LICENSE file in the
repository root.
