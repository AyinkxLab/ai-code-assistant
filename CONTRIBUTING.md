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
- [Running tests](#running-tests)
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
- To point at a local node: set `STELLAR_NETWORK=custom` and
  `STELLAR_HORIZON_URL`/`STELLAR_RPC_URL` to loopback endpoints.
- The test suite uses deterministic fixtures and mocked transport; no real
  network access is required. Add fixtures rather than hitting live nodes.

### Testing Stellar work

```bash
pytest tests/test_soroban_rpc.py tests/test_stellar_xdr.py \
       tests/test_stellar_inspection.py tests/test_stellar_security.py \
       tests/test_stellar_detection.py tests/test_stellar_analysis.py \
       tests/test_stellar_routes.py tests/test_stellar_cli.py
```

## Security reporting

There is currently **no `SECURITY.md`** and GitHub vulnerability alerts are
not enabled on this repository. Until that changes:

- **Report security issues privately and promptly.** Open a GitHub issue with
  the `security` label describing the problem.
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

There is currently **no `CODE_OF_CONDUCT.md`** in this repository. Until one
is added, we expect everyone to treat each other professionally and with
respect: be welcoming to new contributors, give and receive feedback
constructively, and assume good intent. Harassment or abusive behavior is not
acceptable in issues, PRs, or reviews.
