# Reviews & Quality Tooling (Phase 6)

This guide documents the **Reviews** feature end to end: the pages contributors
use, the `REVIEW_*` environment settings that bound every run, and the JSON API
that powers the UI. It is the companion to the feature summary in
[`README.md`](../README.md) and the configuration table there.

An AI review never merges, closes, approves, or otherwise modifies a pull
request, and it never sends more repository text to the model than the
configured bounds allow. Everything is owner-scoped: a review, its findings, and
a project's review configuration are only visible to the user who owns them.

## The Reviews pages

The `reviews` blueprint (`app/reviews/routes.py`) serves four HTML pages:

| Page | Route | Purpose |
| ---- | ----- | ------- |
| Review history | `GET /reviews/` | Quality-metrics strip plus the list of the user's reviews (filter by source/kind/status). |
| Review detail | `GET /reviews/<review_id>` | The stored summary and structured findings for one review; findings can be marked addressed and filtered by severity/category. |
| Project reviews | `GET /reviews/projects/<project_id>` | Run a new `quality`, `security`, or `tests` review over an imported project and see that project's history. |
| Project review config | `GET /reviews/projects/<project_id>/config` | Per-project review configuration (kinds, severity threshold, languages, focus areas, bounds). |

The history list, detail view, and metrics strip are rendered client-side by
`app/static/js/reviews.js`, which calls the JSON API below. Pages are protected
by `@login_required`; the underlying APIs are **owner-scoped** (a review or
project owned by another user returns `404`).

### Workflow

1. **Import a project** (GitHub repo or archive) under **Workspaces**. Project
   reviews require the project to finish indexing (`status == "ready"`);
   otherwise the run is rejected with `409`.
2. **Configure the project** (optional) on the project review config page — pick
   the enabled kinds, severity threshold, language filter, and focus areas.
3. **Run a review** from the project reviews page. A review row is created with
   `status="running"`, the AI service runs, and the result is persisted as a
   summary plus `ReviewFinding` rows. A failed run is stored with
   `status="failed"` and an `error_message` — never silently dropped.
4. **Triage findings** on the review detail page: mark a finding *addressed*, or
   filter by severity/category/confidence. The quality dashboard recomputes its
   metrics from the stored rows only.

### Finding vocabulary

Findings use a normalized vocabulary (see `app/models/review_finding.py`):

- **Severity:** `critical`, `high`, `medium`, `low`, `informational`.
- **Confidence:** `confirmed`, `potential`, `suggestion`. Confirmed findings are
  the ones the code proves; everything else is labelled `[SUGGESTION]` in the UI
  (`confidence_label`).
- **Category:** depends on the review kind — `pr` reviews use bug/security/logic/
  performance/…; `quality` uses readability/complexity/duplication/dead-code/…;
  `security` uses authentication/authorization/injection/secrets/…; `tests` uses
  coverage-gap/missing-assertion/flaky-test/….

A finding carries a file path and line only — never raw repository content.

## Environment settings

All review behaviour is environment-driven and documented in
[`.env.example`](../.env.example). These are the `REVIEW_*` settings:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `REVIEW_MAX_FILES` | `40` | Maximum changed files analyzed in one pull-request review. |
| `REVIEW_MAX_CONTEXT_CHARS` | `40000` | Maximum diff/repository context (characters) sent to the model per review. |
| `REVIEW_MAX_FINDINGS` | `100` | Maximum findings stored per review. |
| `REVIEW_KINDS` | `quality,security,tests` | Default enabled project review kinds (comma-separated). |
| `REVIEW_SEVERITY_THRESHOLD` | `low` | Minimum severity stored for project reviews (`critical`/`high`/`medium`/`low`/`informational`). |

Per-project configuration can narrow the kinds, severity threshold, languages,
focus areas, and bounds, but it can never exceed the `REVIEW_*` caps above.

## Review API reference

Every route below is a JSON API served by `app/reviews/routes.py` and requires a
logged-in user. Errors are JSON bodies of the form `{"error": "<message>"}`.
GitHub errors reuse the sanitized `{"error", "kind"}` payload from
`app/services/github.py` — raw GitHub messages, tokens, and headers are never
returned.

### Reviews

#### List reviews

`GET /reviews/api/reviews` — optional query params `source` (`github_pr` /
`project`), `kind`, `status`, and `project_id`. Returns the user's reviews,
newest first.

```json
[
  {
    "id": 12,
    "source": "project",
    "kind": "security",
    "status": "completed",
    "findings_count": 3,
    "summary": {"overview": "..."},
    "created_at": "2026-01-01T00:00:00+00:00"
  }
]
```

#### Run a review

`POST /reviews/api/reviews` — body selects the source.

Project review:

```json
{"source": "project", "project_id": 3, "kind": "quality"}
```

- `project_id` must be owned by the caller and indexed, else `409`.
- `kind` must be one of `quality` / `security` / `tests`, else `400`.
- Rejected with `400` when the project's config disables reviewing or disables
  that kind.
- Returns `201` with the created review (`status="completed"` or `"failed"`).

Pull-request review:

```json
{"source": "github_pr", "repo": "owner/name", "pr_number": 42, "project_id": 3}
```

- `repo` must be a valid `owner/name`; `pr_number` is required.
- Requires a connected GitHub account; the PR and its files are fetched with the
  user's own token (`400`/`502` on GitHub errors).
- `project_id` is optional and only links the review to a project.
- Returns `201` with the created review (`kind="pr"`).

#### Review detail

`GET /reviews/api/reviews/<review_id>` — the review plus `categories`, the
category vocabulary for its kind (used to build the detail-page filter).

#### Delete a review

`DELETE /reviews/api/reviews/<review_id>` — deletes the review and its findings
(owner only). Returns `{"ok": true}`.

### Findings

#### List findings

`GET /reviews/api/reviews/<review_id>/findings` — optional filters `severity`,
`category`, `confidence`, and `addressed` (`0`/`1`). Returns findings ordered by
severity then id.

#### Mark a finding addressed

`PATCH /reviews/api/reviews/findings/<finding_id>` — body `{"addressed": true}`.
Returns the updated finding.

### Project review configuration

#### Get configuration

`GET /reviews/api/projects/<project_id>/config` — the stored config merged with
application defaults.

#### Update configuration

`PATCH /reviews/api/projects/<project_id>/config` — accepts any of `kinds`,
`severity_threshold`, `languages`, `testing_focus`, `security_focus`,
`performance_focus`, `max_files` (1–200), `max_context_chars` (2000–200000), and
`enabled`. Invalid values are rejected with `400` and the row is rolled back.

```json
{
  "kinds": "quality,tests",
  "severity_threshold": "medium",
  "languages": "python,typescript",
  "max_files": 25,
  "max_context_chars": 40000,
  "enabled": true
}
```

### Quality metrics

`GET /reviews/api/metrics` — aggregate metrics computed strictly from stored
reviews and findings. Scope with `workspace_id` or `project_id`; with neither it
returns metrics for the current user. The payload includes `total_reviews`,
`by_status`, `by_kind`, `by_source`, a `findings` breakdown (totals by severity,
category, confidence, file, addressed/high-risk counts), and
`findings_trend` (per-review open vs addressed counts, oldest first).
