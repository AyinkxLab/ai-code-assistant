# Incident runbook

This runbook covers the alerts in
[`deploy/monitoring/alert-rules.yaml`](../deploy/monitoring/alert-rules.yaml).
Rules are evaluated against the metrics documented in
[Observability](observability.md); objectives live in [SLOs](slos.md).

The repository ships rules and documentation, not a Prometheus server. Load the
rule file into the Prometheus instance that scrapes `/metrics`, and confirm it
parses before relying on it:

```bash
promtool check rules deploy/monitoring/alert-rules.yaml
```

`promtool` reports syntax errors as structured diagnostics with the file, line
and failing expression. Fix the rule; do not silence it.

## First five minutes

1. **Acknowledge** the page and note the alert name, start time, namespace and
   region. Open the `aica-slo-overview` dashboard from `docs/slos.md`.
2. **Decide whether users are affected.** A fast-burn alert means the objective
   is at risk; an error spike or queue backlog may already be user-visible.
3. **Check recent changes.** `kubectl -n aica rollout history deployment/aica-web`
   (or `helm history aica`). A deploy in the last 30 minutes is the first
   suspect, not the metrics backend.
4. **Check dependencies.** Database reachability, provider status, and worker
   health. `GET /health` is liveness only and does not prove database access;
   confirm with a query or the worker probe.
5. **Write down the timeline** as you go: detection, action, result. Use the
   incident channel for anything customer-visible.

Never raise an SLO target, widen an alert window, or remove a probe to make a
page stop. Fix the cause or roll back.

## Availability burn

Alerts: `AicaAvailabilityFastBurn`, `AicaAvailabilitySlowBurn`.

1. Query the failing status classes:

   ```promql
   sum by (status) (rate(http_requests_total[5m]))
   sum by (endpoint) (rate(http_requests_total{status="5xx"}[5m]))
   ```

2. If one endpoint dominates, see [Error spikes](#error-spikes). If all routes
   fail, suspect the database, the session secret, or the app failing to start.
3. Check pod restarts and logs:

   ```bash
   kubectl -n aica get pods
   kubectl -n aica logs deployment/aica-web --since=15m --tail=200
   kubectl -n aica describe deployment/aica-web
   ```

4. If the burn started at a deploy, roll back (see [Rollback](#rollback)) and
   watch the burn rate for one evaluation interval before declaring recovery.
5. If no deploy is involved, check PostgreSQL availability and connection
   limits, then provider status for LLM-backed routes. Database failover is an
   operator step; the application has one writer (see
   [Multi-region deployment](multi-region.md)).

Burn math: allowed error ratio is `1 - 0.995 = 0.005`. A 14.4x multiplier means
the 30-day budget would be consumed in roughly two days; 6x in roughly five.

## Error spikes

Alerts: `AicaHighErrorRate`, `AicaErrorBurstByEndpoint`.

1. Find the route and the first bad response:

   ```promql
   topk(5, sum by (endpoint) (rate(http_requests_total{status="5xx"}[5m])))
   ```

2. Correlate with logs by `X-Request-ID` / `X-Trace-ID` and the slow-query log
   (see [Observability](observability.md)).
3. Reproduce the route in staging if it is not obviously a deploy. Check 4xx
   vs 5xx: 4xx alerts usually mean a client or validation change, not an outage.
4. Roll back the deploy if the error started with it and the fix is not a
   one-line forward change.
5. If a dependency is down, follow [Escalation](#escalation) and consider
   disabling the affected feature rather than failing every request.

## LLM latency

Alert: `AicaLlmLatencyP95High`.

1. Break latency down by provider and model:

   ```promql
   histogram_quantile(0.95, sum by (provider, model, le) (rate(llm_latency_ms_bucket[5m])))
   ```

2. Check the provider's status page. One slow provider is a routing/config
   decision; all providers slow points at the network or the application.
3. Do not raise `LLM_MAX_RETRIES` during an incident. Retries amplify load;
   lower concurrency or fail over to another configured provider instead.
4. Latency without errors is usually a warning, not a rollback trigger. Keep
   the customer impact (slow chat streams) in the incident timeline.

## Queue backlog

Alert: `AicaJobQueueBacklog`.

1. Confirm the worker is running and not crash-looping:

   ```bash
   kubectl -n aica get deployment/aica-worker
   kubectl -n aica logs deployment/aica-worker --since=15m --tail=200
   flask jobs list --status failed --limit 20 --json
   ```

2. Inspect failed jobs' `last_error` (payloads are not printed). A poison job
   retries with backoff until `max_attempts`; a flood of retries can look like
   a backlog. Fix the handler cause rather than raising `max_attempts`.
3. Scale workers only if the database has headroom; every worker claims jobs
   with `FOR UPDATE SKIP LOCKED`, so more workers is safe but adds DB load.
4. If the queue was paused deliberately (`worker.replicaCount: 0`), unpause it
   or note the accepted backlog in the incident timeline.
5. `job_queue_depth` is read from the `jobs` table at scrape time, so a stale
   value is not expected while the database is reachable.

## Webhook failures

Alert: `AicaWebhookDeliveryFailures`.

1. List recent failed webhook jobs:

   ```bash
   flask jobs list --status failed --limit 20 --json
   ```

2. Separate **validation** failures (private/loopback host, non-HTTP scheme,
   credentials in the URL) from **delivery** failures (timeouts, 4xx/5xx from
   the receiver). Validation failures are working as intended; the producer
   needs a new target.
3. Contact the receiving integration owner with the status code and timestamp.
   Do not add the target to an allowlist or disable public-host validation to
   silence the alert.
4. Re-enqueue only after the receiver confirms recovery. Delivery is not
   idempotent from the queue's perspective; duplicate webhooks are possible.

## Rollback

Application rollback:

```bash
# Plain manifests
kubectl -n aica rollout undo deployment/aica-web
kubectl -n aica rollout undo deployment/aica-worker
kubectl -n aica rollout status deployment/aica-web

# Helm
helm history aica --namespace aica
helm rollback aica <revision> --namespace aica
```

Database migrations are **not** rolled back by these commands. Prefer a forward
migration fix; see [Safe migration rollout](migration-rollout.md).

If a rollback does not start within five minutes, or the previous revision is
also failing, escalate.

## Escalation

| When | Escalate to | Channel |
| --- | --- | --- |
| Critical alert not acknowledged in 5 minutes | Primary on-call engineer | Pager (Alertmanager route, configured outside this repo) |
| Availability burn or error spike lasts 15 minutes after acknowledgement | Service owner / backend lead | Incident channel |
| Data loss, security exposure, or database unavailable | Platform lead and security owner | Incident channel + direct page |
| Customer-visible incident over 30 minutes | Engineering manager and support lead | Incident channel; support updates customers |

Escalation is time-boxed, not optional: if the current responder is unsure, page
the next role. Include the alert name, start time, current impact, actions taken
and the rollback revision in the handoff.

## Closing the incident

- Confirm the alert is `inactive` and the SLO burn rate is back under the
  threshold for at least one full evaluation window.
- Record the timeline, root cause, rollback/fix revision, and follow-up issues.
- Add a regression test or alert for the failure mode when possible.
- Do not delete the incident channel; link it from the follow-up issue.
