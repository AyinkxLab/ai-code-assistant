# Performance budgets (Phase 12, #702)

The repository runs a small, deterministic benchmark in CI to catch a
regression in query count or response latency on the hottest workspace list
endpoints. Budgets are declared in
[`perf_budgets.json`](../perf_budgets.json) and the check lives in
[`scripts/perf_budget.py`](../scripts/perf_budget.py). For a repeated-load
smoke test of the main API and dashboard paths, see
[`load-testing.md`](load-testing.md).

## What is measured

The benchmark seeds a fixed in-memory SQLite database (200 plugins and 500
plugin execution audit rows), logs in one owner, warms each request, and then
measures the second call:

| Scenario | Request |
| --- | --- |
| `plugins_list` | `GET /plugins/api/workspaces/<id>/plugins?limit=50` |
| `workspace_audit` | `GET /workspaces/api/workspaces/<id>/audit?per_page=50` |
| `plugin_executions` | `GET /plugins/api/workspaces/<id>/plugin-executions?limit=50` |

* **Queries** are counted with a SQLAlchemy `before_cursor_execute` listener.
  This is deterministic for a fixed seed and page size. The `plugins_list`
  baseline includes the app's existing per-plugin installation/capability
  lookups (about four queries per returned plugin), so the documented budget is
  `210` rather than a single-digit number. A change that removes that N+1 work
  only lowers the observed count; a change that adds queries fails the budget.
* **Latency** is measured in-process with `perf_counter` and uses a deliberately
  generous budget (`2000 ms`) so shared-runner noise does not fail the build.
  Query count is the primary regression signal.

## Advisory mode

The check starts **advisory**: a budget regression is printed, but the process
exits `0` so the baseline can be observed for a few weeks before it becomes a
required gate.

```bash
# Advisory (default): report regressions, exit 0
python scripts/perf_budget.py --advisory

# Enforced: exit 1 when any documented budget is exceeded
python scripts/perf_budget.py --enforce

# Or set the environment flag used by CI
PERF_BUDGETS_ENFORCE=1 python scripts/perf_budget.py
```

`--advisory` always wins over `--enforce` and the environment flag, so the CI
job can stay explicitly advisory until a maintainer flips it. The JSON
document's `enforce` key is descriptive; the CLI flag/environment variable is
authoritative.

## Changing a budget

1. Run `python scripts/perf_budget.py --advisory` and record the four numbers.
2. If the increase is intentional, update the matching scenario in
   `perf_budgets.json` in the same pull request and explain why in the PR.
3. Do not raise a latency budget to hide a query-count regression — fix the
   query or pass a larger page-size test deliberately.

The benchmark must remain deterministic and offline: no network, no wall-clock
sleeps, and no unbounded seed size. Tests for the evaluator and advisory/enforce
modes live in [`tests/test_perf_budgets.py`](../tests/test_perf_budgets.py).
