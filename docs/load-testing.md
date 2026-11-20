# Load-test harness (#701)

`scripts/load_test.py` is a repeatable local load test for the main API and
dashboard paths. It uses only the standard library (`threading`, `urllib`) plus
the application's existing Werkzeug server. There is no new dependency and no
external service.

## What it does

1. Creates a temporary SQLite database and seeds a deterministic user, one
   workspace, 10 conversations, 5 plugins and 7 usage rows.
2. Creates a signed session for the seeded user and starts the Flask app on a
   random loopback port (Werkzeug's threaded server).
3. Fires a fixed number of `GET` requests across the selected paths with a
   bounded worker pool, recording latency and structured failures.
4. Prints p50/p95/p99, max/mean latency, requests per second and errors grouped
   by path, status and code. Exits `1` when the error rate exceeds
   `--max-error-rate` (default `0.0`, so any failure fails the run).

Paths:

| Scenario | Paths |
| --- | --- |
| `main` | `GET /health`, `GET /api/conversations` |
| `dashboard` | `GET /billing/api/usage/dashboard?days=30` |
| `all` (default) | every path above |

The dashboard path uses the seeded owner session, so it exercises the real
dashboard query rather than the login redirect.

## Running manually

From the repository root, with the virtual environment active:

```bash
# Small CI-sized run (default: 40 requests, 4 workers)
python scripts/load_test.py

# Longer manual run (200 requests, 8 workers)
python scripts/load_test.py --profile manual

# Explicit sizes, machine-readable output
python scripts/load_test.py --requests 100 --concurrency 8 --json

# Main API only
python scripts/load_test.py --scenario main
```

A run prints one summary line and any error groups:

```text
requests=6 concurrency=2 errors=0 p50=30.473ms p95=151.066ms p99=154.328ms rps=29.89
```

`--json` prints the full report instead:

```json
{
  "requests": 6,
  "successes": 6,
  "errors": {"count": 0, "rate": 0.0, "items": []},
  "latency_ms": {"p50": 30.473, "p95": 151.066, "p99": 154.328, "max": 154.9, "mean": 31.2},
  "by_path": [{"path": "/health", "requests": 2, "p50": 12.0, "p95": 12.4, "p99": 12.5}],
  "requests_per_second": 29.89
}
```

Percentiles are linearly interpolated between sorted samples. With one sample
every percentile is that sample; with no samples they are `null`.

## Running against an existing server

Pass `--base-url` and the session cookie header. The cookie is the value of the
Flask `session` cookie from a signed-in browser (or from a test client):

```bash
python scripts/load_test.py \
  --base-url http://127.0.0.1:5000 \
  --cookie "session=<value>" \
  --requests 200 --concurrency 8
```

`LOAD_TEST_COOKIE` can supply the cookie instead. Do not commit or paste a real
session cookie into an issue; it is a credential.

## Running in CI

The default profile is deliberately small (40 requests, 4 workers) and the
database is temporary, so it is safe as an optional CI smoke step:

```bash
python scripts/load_test.py --requests 40 --concurrency 4
```

It is not part of the required test job in this change: latency numbers are
environment-dependent and can be noisy on shared runners. Run it manually
before a release, or add it as a non-blocking workflow job with the default
profile. The unit tests for the reporting logic (`tests/test_load_harness.py`)
do run in the normal suite.

## Limits and honest reading

- This measures the local single-process app and SQLite/file database, not
  production capacity. The seeded workload is tiny.
- Each request uses a fresh urllib connection; there is no keep-alive tuning.
- The in-process Prometheus registry and any process-local caches are shared by
  the workers, which is representative of one worker only.
- Latency includes SQLite writes from the session/login middleware's audit path
  where applicable; compare runs on the same machine and profile.
- Structured error bodies are truncated to 200 characters and never include
  request headers or cookies.

Implementation: `scripts/load_test.py`. Tests:
`tests/test_load_harness.py`.
