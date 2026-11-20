"""Deterministic query-count and latency budget check (issue #702).

The benchmark seeds a fixed in-memory database and times three representative
workspace list endpoints. Query counts are deterministic; latency is measured
in-process and kept well above CI noise. Budgets live in ``perf_budgets.json``
and are documented in ``docs/performance-budgets.md``.

The check starts in **advisory mode**: a regression is printed and the process
still exits ``0``. Once the baseline is stable, run with ``--enforce`` (or set
``PERF_BUDGETS_ENFORCE=1``) so a regression exits ``1`` and fails CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT))

DEFAULT_BUDGET_FILE = ROOT / "perf_budgets.json"
PLUGIN_COUNT = 200
AUDIT_COUNT = 500

SCENARIOS = {
    "plugins_list": "/plugins/api/workspaces/{workspace_id}/plugins?limit=50",
    "workspace_audit": "/workspaces/api/workspaces/{workspace_id}/audit?per_page=50",
    "plugin_executions": ("/plugins/api/workspaces/{workspace_id}/plugin-executions?limit=50"),
}


def load_budgets(path: Path = DEFAULT_BUDGET_FILE) -> dict[str, Any]:
    """Read the budget document."""
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(
    measurements: dict[str, dict[str, float]], budgets: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return one failure per exceeded metric (empty when all budgets pass)."""
    failures = []
    scenarios = budgets.get("scenarios", {})
    for name, observed in measurements.items():
        budget = scenarios.get(name)
        if not budget:
            continue
        for metric in ("queries", "latency_ms"):
            limit = budget.get(metric)
            if limit is None:
                continue
            actual = observed.get(metric, 0)
            if actual > limit:
                failures.append(
                    {
                        "scenario": name,
                        "metric": metric,
                        "observed": actual,
                        "budget": limit,
                    }
                )
    return failures


def resolve_enforce(explicit: bool, advisory: bool) -> bool:
    """Resolve enforcement: explicit flags win, then the environment."""
    if advisory:
        return False
    return explicit or os.getenv("PERF_BUDGETS_ENFORCE") == "1"


def _seed(app):
    from app.extensions import db
    from app.models import ActivityEvent, Plugin, User, Workspace
    from app.models.activity_event import EVENT_PLUGIN_EXECUTED

    with app.app_context():
        db.create_all()
        user = User(username="perf", email="perf@example.com")
        user.set_password("supersecret123")
        db.session.add(user)
        db.session.commit()
        workspace = Workspace(user_id=user.id, name="Perf")
        db.session.add(workspace)
        db.session.commit()

        db.session.bulk_save_objects(
            [
                Plugin(
                    id=f"perf-plugin-{index:04d}",
                    name=f"Perf plugin {index}",
                    version="1.0.0",
                    entry_point="perf:Perf",
                    capabilities=[],
                    enabled=True,
                )
                for index in range(PLUGIN_COUNT)
            ]
        )
        db.session.bulk_save_objects(
            [
                ActivityEvent(
                    workspace_id=workspace.id,
                    event_type=EVENT_PLUGIN_EXECUTED,
                    event_metadata={
                        "plugin_id": f"perf-plugin-{index:04d}",
                        "version": "1.0.0",
                        "capability": "project:read",
                        "outcome": "success",
                        "duration_ms": 1,
                    },
                )
                for index in range(AUDIT_COUNT)
            ]
        )
        db.session.commit()
        return workspace.id


def _login(client) -> None:
    response = client.post(
        "/auth/login",
        data={"email": "perf@example.com", "password": "supersecret123"},
        follow_redirects=False,
    )
    if response.status_code not in (302, 303):
        raise RuntimeError(f"Benchmark login failed ({response.status_code}).")


def _measure(client, url: str, engine) -> dict[str, float]:
    from sqlalchemy import event

    counter = {"queries": 0}

    def _before(*_args) -> None:
        counter["queries"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    started = time.perf_counter()
    try:
        response = client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", _before)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if response.status_code != 200:
        raise RuntimeError(f"Benchmark request {url} returned {response.status_code}.")
    return {"queries": counter["queries"], "latency_ms": round(elapsed_ms, 3)}


def run_benchmark() -> dict[str, dict[str, float]]:
    """Run the fixed benchmark and return per-scenario measurements."""
    from app import create_app

    app = create_app("testing")
    workspace_id = _seed(app)
    client = app.test_client()
    _login(client)

    from app.extensions import db

    with app.app_context():
        engine = db.engine
    measurements = {}
    for name, template in SCENARIOS.items():
        url = template.format(workspace_id=workspace_id)
        client.get(url)  # warm-up: template/Jinja/session caches
        measurements[name] = _measure(client, url, engine)
    return measurements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enforce", action="store_true", help="Fail on a regression.")
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="Force advisory mode even if PERF_BUDGETS_ENFORCE=1 is set.",
    )
    parser.add_argument("--budgets", type=Path, default=DEFAULT_BUDGET_FILE)
    args = parser.parse_args(argv)

    budgets = load_budgets(args.budgets)
    measurements = run_benchmark()
    failures = evaluate(measurements, budgets)
    enforce = resolve_enforce(args.enforce, args.advisory)
    mode = "enforced" if enforce else "advisory"

    for name, observed in measurements.items():
        budget = budgets.get("scenarios", {}).get(name, {})
        print(
            f"{name}: queries={observed['queries']} "
            f"(budget {budget.get('queries', '-')}), "
            f"latency={observed['latency_ms']:.1f}ms "
            f"(budget {budget.get('latency_ms', '-')}ms)"
        )

    if failures:
        print(f"perf budget mode={mode}: {len(failures)} regression(s)")
        for failure in failures:
            print(
                f"  {failure['scenario']}.{failure['metric']}: "
                f"{failure['observed']} > {failure['budget']}"
            )
    else:
        print(f"perf budget mode={mode}: all budgets met")

    if failures and enforce:
        return 1
    if not enforce:
        print(
            "advisory: regressions do not fail CI; pass --enforce "
            "(or set PERF_BUDGETS_ENFORCE=1) once the baseline is stable."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
