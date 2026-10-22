# Billing

Cost figures in this application are **estimates** derived from token counts and
an operator-verifiable price table. They are useful for budgets, dashboards, and
chargeback reporting; they are not a substitute for a provider invoice. The
append-only [usage ledger](observability.md#usage-ledger) is the source of truth
for what was recorded.

## Price table

`app/services/pricing.py` ships conservative defaults in USD per 1,000 tokens:

| Provider | Model | Input / 1k | Output / 1k | Estimated? |
| -------- | ----- | ---------- | ----------- | ---------- |
| `mock` | `mock-1` | 0 | 0 | No (offline, free) |
| `openai` | `gpt-4o-mini` | 0.00015 | 0.0006 | Yes |
| `openai` | `gpt-4o` | 0.0025 | 0.01 | Yes |
| `openai` | `gpt-4.1-mini` | 0.0004 | 0.0016 | Yes |
| `openai` | `gpt-4.1` | 0.002 | 0.008 | Yes |
| `anthropic` | `claude-3-5-sonnet-latest` | 0.003 | 0.015 | Yes |
| `anthropic` | `claude-3-5-haiku-latest` | 0.0008 | 0.004 | Yes |

Lookup rules:

- provider/model matching is case-insensitive and ignores surrounding
  whitespace;
- exact entries win over `(provider, "*")`, `("*", model)`, and `("*", "*")`
  fallbacks;
- an unknown combination returns `None` (cost unknown) rather than guessing
  zero;
- per-workspace overrides replace matching global entries and always take
  precedence, including wildcard overrides.

Costs are rounded with `ROUND_HALF_UP` to six decimal places
(`COST_PRECISION`) — see [Cost precision](cost-precision.md). Token counts are
clamped to `MAX_TOKEN_COUNT` before costing so implausible input stays finite.

### Configuring the table

`LLM_PRICE_TABLE` accepts a JSON overlay keyed by `provider/model`; entries not
listed keep the shipped defaults:

```json
{
  "openai/gpt-4o": {"input_per_1k_usd": 0.0025, "output_per_1k_usd": 0.01},
  "mock/*": {"input_per_1k_usd": 0.0, "output_per_1k_usd": 0.0}
}
```

Each entry must be an object with non-negative finite `input_per_1k_usd` and
`output_per_1k_usd` numbers. Malformed JSON, a bad key, or an invalid rate is
reported at startup (`PRICE_TABLE_ERROR` + an error log) and the application
falls back to the shipped estimates; requests are never broken by a bad table.
`app/services/cost.py` turns a usage record into a labelled estimate
(`cost_is_estimate`, `token_source: reported|estimated`, `priced`), returning
`None` for unknown models. Pre-flight estimates use the table when `LLM_MODEL`
is set, otherwise the legacy `LLM_ESTIMATE_USD_PER_1K_TOKENS` rate is kept.

### Workspace price overrides

Workspace owners can manage provider/model overrides (issue #442):

| Route | Access | Purpose |
| ----- | ------ | ------- |
| `GET /workspaces/api/workspaces/<id>/pricing` | workspace member | Effective price table for the workspace. |
| `GET /workspaces/api/workspaces/<id>/pricing/overrides` | owner | Effective table plus explicit override rows. |
| `PUT /workspaces/api/workspaces/<id>/pricing/overrides` | owner | Create or update one or more overrides. |
| `DELETE /workspaces/api/workspaces/<id>/pricing/overrides/<provider>/<model>` | owner | Remove one override. |

Overrides are billing data, so reads of the explicit rows are owner-only.
Implementation: `app/services/pricing.py`,
`app/models/workspace_price_override.py`; tests: `tests/test_pricing.py`,
`tests/test_workspace_pricing_api.py`.

## Budgets

Budgets are monthly, scoped to exactly one user **or** one workspace, and may
limit tokens, estimated cost, or both (`app/models/budget.py`).

- `token_limit` caps total tokens recorded in the month.
- `cost_limit` caps estimated cost in the configured currency.
- `warn_threshold` (default `0.8`, i.e. 80%) triggers a warning notification
  before the limit is reached. It is configured per budget and also returned as
  `warn_threshold_pct` for clients that present a percentage. A warning is
  advisory: the request still runs and only adds a header.
- Enforcement runs **before** an LLM call and is shared by every billable path
  (chat, project chat, reviews, project analysis, the AI tools and GitHub AI
  analysis). All of them go through the same `budget_guard` /
  `budget_refusal` decision point in `app/services/budgets.py`; a blocked call
  never reaches the provider. A blocked call returns `429` with
  `kind: "budget_exceeded"`, `limits_hit`, `retry_after`, the evaluated budgets,
  and a `Retry-After` header. Evaluation failures fail closed with `503`
  (`kind: "budget_check_failed"`).
- Allowed calls that cross the warning threshold add `X-Budget-Warning:
  approaching_limit` to the response.
- `denied` ledger rows are excluded from budget totals, so a blocked caller
  cannot increase their own spend.
- Alerts are advisory and de-duplicated per budget/period/level (process-local).
- Budget changes are audited (`BUDGET_UPDATED` / `BUDGET_DELETED`).

Management API (blueprint prefix `/billing`):

| Route | Access | Purpose |
| ----- | ------ | ------- |
| `GET /billing/api/budgets` | signed-in user | Caller's budget plus budgets for owned workspaces, with live usage ratios. |
| `PUT` / `POST /billing/api/budgets` | signed-in user | Upsert a user budget, or a workspace budget when the caller has `manage_billing`. |
| `DELETE /billing/api/budgets/<id>` | owner | Remove a budget the caller owns/manages. |

Temporary overrides and reconciliation helpers live in
`app/services/budget_overrides.py` and `app/services/budget_alerts.py`.
Implementation: `app/services/budgets.py`, `app/billing/routes.py`; tests:
`tests/test_budgets.py`, `tests/test_budget_e2e.py`,
`tests/test_budget_overrides.py`, `tests/test_alerts.py`.

## Provider billing reconciliation

`POST /billing/import` accepts a bounded CSV of what a provider actually charged
and compares it with the app's token-based estimate. `GET
/billing/api/reconciliation` returns the report, optionally filtered by
`?period=YYYY-MM`.

Accepted columns:

- provider: `provider` or `vendor`;
- period: `period` (`YYYY-MM`);
- amount: `amount_usd`, `cost`, `cost_usd`, `amount`, or `usd`.

Bounds: `BILLING_IMPORT_MAX_BYTES` (default 1 MiB) and
`BILLING_IMPORT_MAX_ROWS` (default 5,000). Import is validated row by row,
idempotently upserts `(user, provider, period)`, and reports reported total,
estimated total, and variance. Estimates are computed fresh from assistant
messages and are never overwritten by an import.

Implementation: `app/services/billing_reconciliation.py`,
`app/models/provider_billing_record.py`; tests:
`tests/test_billing_reconciliation.py`.

## Recomputing historical costs

When a price or workspace override changes, recompute stored estimates with the
usage CLI:

```bash
flask usage recompute --limit 5000 --batch-size 500
flask usage recompute --workspace-id 7 --dry-run --json
```

The command is bounded and idempotent: it processes keyset batches, writes only
rows whose estimate differs, and stamps `cost_recomputed_at` on each changed
row. Unknown provider/model combinations are left as `NULL` rather than zero.
See [Observability § Cost recomputation](observability.md#cost-recomputation).

## Exports

Usage exports are documented in
[Observability § Usage export](observability.md#usage-export). CSV exports (user,
workspace, and org-wide) are bounded to 5,000 rows / 2 MiB and set
`X-Export-Truncated: true` when clipped.

## Configuration

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `CURRENCY_CODE` | `USD` | Currency code shown next to every cost figure. |
| `CURRENCY_SYMBOL` | `$` | Display symbol. |
| `CURRENCY_DECIMAL_PLACES` | `4` | Display precision (stored costs use 6 decimals). |
| `LLM_ESTIMATE_USD_PER_1K_TOKENS` | `0.002` | Fallback rate used when no `LLM_MODEL` is configured and by billing reconciliation. |
| `LLM_MODEL` | empty | Pre-flight estimate model; when set, the provider/model price table is used. |
| `LLM_PRICE_TABLE` | empty | Optional JSON overlay for provider/model prices. Invalid configuration logs an error, sets `PRICE_TABLE_ERROR`, and falls back to defaults. |
| `BUDGET_ENFORCEMENT_ENABLED` | `1` | Evaluates budgets before LLM calls; disable only for local debugging. |
| `BUDGET_DEFAULT_WARN_THRESHOLD` | `0.8` | Default warning fraction for new budgets. |
| `BILLING_IMPORT_MAX_BYTES` | `1048576` | Maximum billing CSV upload size. |
| `BILLING_IMPORT_MAX_ROWS` | `5000` | Maximum billing CSV rows. |
| `USAGE_RETENTION_DAYS` | `365` | Ledger retention window for `flask usage purge`. |

Keep the currency code and the price/estimate rates in the same currency.
Changing the code does not convert stored values.
