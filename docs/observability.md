# Observability

The application records usage and cost data in an append-only ledger, exposes
bounded Prometheus counters, and renders dashboards from stored rows. Nothing in
this surface stores prompts, completions, or secrets — see
[Usage and billing data privacy](usage-privacy.md).

## Data flow

1. **Instrumentation.** LLM-backed paths append a `UsageRecord` through
   `app/services/usage_ledger.py`. The same call updates the in-process token and
   cost counters. Every request also increments an HTTP counter via the
   `after_request` hook registered by `app/observability/__init__.py`.
2. **Aggregation.** Dashboards, exports, budgets, and cost estimates compute
   from the ledger on read. The ledger is the single source of truth.
3. **Exposition.** `/metrics` serves Prometheus text; `/billing/*`, `/admin/*`,
   and `/api/usage/*` serve JSON/CSV dashboards and exports. Chat audit logging
   emits security-relevant events separately.

No read endpoint mutates the ledger.

## Usage ledger

`UsageRecord` (`app/models/usage_record.py`) stores metadata only:

| Column | Meaning |
| ------ | ------- |
| `user_id`, `workspace_id`, `project_id`, `conversation_id` | Optional scope. Rows follow the caller's ownership checks. |
| `source` | Operation family: `chat`, `project_chat`, `analysis`, `review`, `tools`, or `unknown`. |
| `provider`, `model` | Provider/model labels; unknown combinations are kept as `unknown`. |
| `prompt_tokens`, `completion_tokens`, `total_tokens`, `tokens_estimated` | Token counts and whether they were estimated. |
| `cost_estimate` | Estimated cost in the configured display currency, or `NULL` when the price is unknown. |
| `request_id`, `latency_ms`, `status` | Correlation/performance metadata and `success`/`error`/`denied`. |
| `created_at` | UTC timestamp. |

The ledger is append-only. `flask usage backfill` imports historical assistant
messages in bounded, idempotent batches; `flask usage purge` removes rows older
than `USAGE_RETENTION_DAYS`. Denied calls do not count toward budgets.

Reads filter on a bounded `created_at` window plus an optional scope column and
group by `source`/`provider`/`model`. Composite indexes
(`ix_usage_records_<scope>_created`, `ix_usage_records_source_created`,
`ix_usage_records_provider_model_created`, `ix_usage_records_status_created`)
serve those shapes; `tests/test_usage_ledger_indexes.py` pins the query plans.

## Metrics

`GET /metrics` returns Prometheus `text/plain; version=0.0.4` and is bounded by
`MAX_SERIES_PER_METRIC` (500) per counter or histogram; overflow is folded into
an `other` series so a hostile label cannot grow memory without limit.

| Metric | Type | Labels | Meaning |
| ------ | ---- | ------ | ------- |
| `http_requests_total` | counter | `method`, `endpoint`, `status` | Requests by route pattern and status class. |
| `llm_tokens_total` | counter | `provider`, `model`, `direction` | Tokens consumed (`in`/`out`). |
| `llm_cost_usd_total` | counter | `provider`, `model` | Estimated cost accumulated from ledger writes. |
| `llm_latency_ms` | histogram | `provider`, `model` | Provider latency in milliseconds; buckets run from 5 ms to 30 s plus `+Inf`. |

Token, cost and latency values are published from `usage_ledger.record_usage`, so
every instrumented path reports the same numbers. Latency is observed only when
the provider reports `latency_ms`; a missing value is not recorded as zero.

Endpoint protection:

- `METRICS_TOKEN` set → requires `Authorization: Bearer <token>`
  (constant-time comparison).
- `METRICS_TOKEN` empty → only an authenticated site-admin session may read it.
- `METRICS_ENABLED=0` → `404`.
- Responses are `Cache-Control: no-store`.

The registry is in-process: counters reset when the process restarts and are not
shared across workers. Scrape each worker, or aggregate in your metrics backend.

## Dashboards

| Endpoint | Access | Description |
| -------- | ------ | ----------- |
| `GET /api/usage` | signed-in user | Current user's usage summary. |
| `GET /api/usage/breakdown` | signed-in user | Totals grouped by source/provider/model. |
| `GET /api/usage/top` | signed-in user | Most expensive conversations/projects; `limit` ≤ 50. |
| `GET /billing/usage` | signed-in owner | HTML usage page; `workspace_id` selects an owned workspace. |
| `GET /billing/api/usage/dashboard` | signed-in owner | JSON totals, breakdowns, daily series; `days` is 1–365. |
| `GET /billing/api/usage/dashboard.csv` | signed-in owner | Bounded CSV export. |
| `GET /billing/api/workspaces/<id>/usage` | workspace owner or site admin | Workspace-scoped dashboard; other callers get a uniform `404`. |
| `GET /billing/api/workspaces/<id>/usage.csv` | workspace owner or site admin | Workspace-scoped CSV export. |
| `GET /admin/usage` | site admin | Org-wide totals and per-user breakdown. |
| `GET /admin/usage/export.csv` | site admin | Org-wide bounded CSV export. |

Empty scopes return zero totals and empty breakdowns with the same shape, so
clients render empty states without guessing. CSV exports stop at 5,000 rows or
2 MiB, whichever comes first, and set `X-Export-Truncated: true` when clipped.

`GET /api/usage/breakdown` also returns bounded provider/model groups:
`by_provider`/`providers` and `by_model`/`models`, sorted by estimated cost,
then total tokens, then label. Rows with no provider/model are grouped under
`unknown`. `limit` defaults to 20 and is clamped to 100; overflow is merged into
an explicit `other` row and reported via `provider_truncated`/`model_truncated`
and `*_groups_total`. Source rows keep their stable all-known-sources shape.

## Cost recomputation

Price-table changes do not rewrite history automatically. Run the bounded,
idempotent recompute command after updating a price or workspace override:

```bash
flask usage recompute --limit 5000 --batch-size 500
flask usage recompute --workspace-id 7 --dry-run --json
```

Only rows whose recomputed value differs are written, and each changed row gets
`cost_recomputed_at`. A second run updates nothing. Rows whose provider/model
has no known price become `NULL` (never zero). Use `--user-id`/`--workspace-id`
to narrow the scope, `--limit` to cap scanned rows, and `--dry-run` to preview.

## Usage export

`GET /api/usage/export` returns a paginated JSON document scoped to the current
user. `workspace_id`, `project_id`, and `source` only narrow that user's rows —
they can never expose another account's usage. `page` starts at 1 and `limit` is
clamped to 1–500.

The document contains:

- `schema_version`, `exported_at`, and the applied `filters`;
- `pagination` (`page`, `limit`, `max_limit`, `total`, `pages`, `has_next`,
  `has_prev`);
- `totals` for the full filtered set and `page_totals` for the page;
- `records` serialized with the ledger serializer;
- `currency` and `cost_is_estimate: true`.

`GET /api/usage/export.csv` (issue #454) returns the same user-scoped ledger as
streamed CSV for billing. It accepts the same `workspace_id`, `project_id`, and
`source` filters, uses the standard CSV writer so commas/quotes/newlines are
escaped, and is capped at `USAGE_EXPORT_CSV_MAX_ROWS` (default 5,000; the cap
can only be lowered). When rows are omitted the response sets
`X-Export-Truncated: true`; `X-Export-Rows` reports the emitted row count. The
column order is stable (`EXPORT_CSV_COLUMNS` in `app/services/usage_ledger.py`).

## Configuration

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `METRICS_ENABLED` | `1` | Hides `/metrics` (404) when disabled. |
| `METRICS_TOKEN` | empty | Bearer token for scrapers. Empty falls back to admin-session auth. |
| `USAGE_RETENTION_DAYS` | `365` | Minimum age before `flask usage purge` may delete ledger rows. |
| `USAGE_EXPORT_CSV_MAX_ROWS` | `5000` | CSV row cap for `/api/usage/export.csv`; values above 5,000 are clamped. |
| `BILLING_IMPORT_MAX_BYTES` | `1048576` | Maximum accepted billing CSV upload size. |
| `BILLING_IMPORT_MAX_ROWS` | `5000` | Maximum accepted billing CSV rows. |
| `CURRENCY_CODE` / `CURRENCY_SYMBOL` / `CURRENCY_DECIMAL_PLACES` | `USD` / `$` / `4` | Display currency for cost figures. |

## Implementation and tests

- Ledger: `app/services/usage_ledger.py`, `app/models/usage_record.py`.
  Tests: `tests/test_usage_ledger.py`, `tests/test_usage_export.py`.
- Metrics: `app/observability/metrics.py`, `app/observability/routes.py`.
  Tests: `tests/test_metrics.py`, `tests/test_metrics_from_stored_rows.py`.
- Dashboards and CSV: `app/services/usage_dashboards.py`,
  `app/billing/routes.py`, `app/admin/routes.py`.
  Tests: `tests/test_usage_dashboards.py`, `tests/test_dashboard_authorization.py`.
- Budgets and cost: see [Billing](billing.md).
