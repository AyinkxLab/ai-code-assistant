# Service-level objectives and dashboards (#704)

This document defines the availability, error-rate and latency objectives for
the application and the dashboard/alert definitions that consume the metrics it
already emits. The machine-readable definitions live in
[`app/observability/slo_definitions.json`](../app/observability/slo_definitions.json)
and are validated by `tests/test_observability_slo.py` against the live metric
registry.

There is no bundled Grafana or Prometheus server. The application exposes
Prometheus text at `GET /metrics`; the JSON file is imported into whatever
backend an operator already runs.

## Objectives

| ID | Objective | Target | Measurement |
| --- | --- | --- | --- |
| `availability` | Non-5xx responses | **≥ 99.5%** over 30 days | `1 - sum(rate(http_requests_total{status="5xx"})) / sum(rate(http_requests_total))` |
| `error_rate` | HTTP 5xx share | **≤ 1%** over 30 days | same ratio, without subtracting from 1 |
| `llm_latency_p95` | LLM provider latency p95 | **≤ 10,000 ms** over 30 days | `histogram_quantile(0.95, sum by (le) (rate(llm_latency_ms_bucket)))` |

The targets are intentionally modest starting points for a single-region
deployment. Tighten them only after the baseline is measured; see
[Alert response](#alert-response).

## Metrics contract

Every definition may reference only these metric families. The contract is
pinned in the JSON file and checked in tests.

| Metric | Type | Labels | Emitted by |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `endpoint`, `status` | `app/observability/__init__.py` (`after_request`) |
| `llm_tokens_total` | counter | `provider`, `model`, `direction` | `app/services/usage_ledger.py` |
| `llm_cost_usd_total` | counter | `provider`, `model` | `app/services/usage_ledger.py` |
| `llm_latency_ms` | histogram | `provider`, `model` | `app/services/usage_ledger.py` |
| `job_queue_depth` | gauge | `status` | `app/services/job_queue.py` |
| `webhook_deliveries_total` | counter | `result` | `app/services/job_queue.py` |

`status` is a status *class* (`1xx` … `5xx`), never an individual code.
Histogram samples appear as `llm_latency_ms_bucket`, `llm_latency_ms_sum` and
`llm_latency_ms_count`. The registry caps series per metric at 500 and folds
overflow into an `other` series, so label values are safe to aggregate but not
guaranteed to be exhaustive.

## Wiring

1. **Scrape the application.** Point Prometheus (or an agent) at
   `GET /metrics` once per worker. With `METRICS_TOKEN` set, send
   `Authorization: Bearer <token>`; otherwise the scraper needs a site-admin
   session. A minimal scrape config:

   ```yaml
   scrape_configs:
     - job_name: ai-code-assistant
       metrics_path: /metrics
       authorization:
         credentials: "<METRICS_TOKEN>"
       static_configs:
         - targets: ["assistant.internal:5000"]
   ```

   The registry is per process. Scrape every worker (or aggregate in the
   backend); do not assume the numbers are cluster-wide.

2. **Import the dashboard.** The `dashboard` object in
   `slo_definitions.json` is a Grafana-style dashboard (panels, targets,
   `refId`, `expr`, `legendFormat`). Choose **Dashboards → New → Import**, paste
   the object, and pick the Prometheus data source. The `uid` is stable
   (`aica-slo-overview`) so repeated imports update the same dashboard.

3. **Load the alert rules.** The operator-facing ruleset is
   [`deploy/monitoring/alert-rules.yaml`](../deploy/monitoring/alert-rules.yaml).
   It includes availability fast/slow burn, the error-rate and per-endpoint
   alerts, LLM latency, queue backlog and webhook failures. Check and reload it:

   ```bash
   promtool check rules deploy/monitoring/alert-rules.yaml
   ```

   The `alerts` array in `slo_definitions.json` remains the source for the
   dashboard-adjacent SLO alerts; `tests/test_observability_alerts.py` asserts
   rules present in both files stay identical. Reload Prometheus and confirm the
   rules are `inactive`/`pending` in **Status → Rules**.

4. **Follow the runbook.** Triage, rollback and escalation steps for every rule
   are in [`incident-runbook.md`](incident-runbook.md).
   `python -m pytest tests/test_observability_slo.py -q`. Adding, renaming or
   removing a metric without updating the definitions (or the reverse) fails
   the test with the unknown name.

## What is not covered

- **HTTP request latency.** Only LLM provider latency is a histogram today, so
  the latency objective covers provider calls, not route latency.
- **Saturation and capacity.** There are no CPU, memory, connection-pool or
  database metrics, so no saturation objective or alert is defined.
- **Background-job latency.** `job_queue_depth` covers backlog counts, not how
  long the oldest queued job has waited; queue lag itself is not measured.
- **Multi-worker aggregation.** The in-process registry is per worker and
  resets on restart. Query results are only as complete as the scrape coverage.
- **Business and cost objectives.** Token and cost series are charted, but no
  spend SLO or budget alert is defined here (budget alerts live in
  [`billing.md`](billing.md)).
- **Traces and logs.** Dashboards are metric-only; see
  [`tracing.md`](tracing.md) for correlation.

## Alert response

1. Check the dashboard's **Requests by status class** and **HTTP 5xx error
   rate** panels to confirm the alert is not a single bad scrape.
2. Query `sum by (endpoint) (rate(http_requests_total{status="5xx"}[5m]))` to
   find the failing route. Per-endpoint error reports are bounded in the
   application; use the request id from `X-Request-ID` when correlating logs.
3. For latency alerts, check `llm_latency_ms` by `provider`/`model` and whether
   the provider is degraded; retries are bounded by `LLM_MAX_RETRIES`.
4. Do not raise a target to silence an alert. Fix the cause, or narrow the
   window in the definition and document why.

## Implementation and tests

- Definitions: `app/observability/slo_definitions.json`.
- Validator: `app/observability/slo.py`.
- Tests: `tests/test_observability_slo.py` (validates every objective, panel
  and alert expression against `REGISTRY.names()`).
- Operator rules: `deploy/monitoring/alert-rules.yaml`; syntax, labels,
  annotations and runbook anchors are checked by
  `tests/test_observability_alerts.py`.
- Incident procedure: [`incident-runbook.md`](incident-runbook.md).
- Metric source: [`observability.md`](observability.md).
