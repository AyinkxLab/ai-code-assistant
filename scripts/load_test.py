"""Repeatable local load test for the main API and dashboard paths (#701).

Standard library only: a seeded SQLite database, a Werkzeug server thread and
``urllib`` workers. The harness records per-request latency and structured
errors, reports p50/p95/p99, and exits non-zero when the error rate exceeds the
configured limit. See ``docs/load-testing.md``.

Examples::

    python scripts/load_test.py --scenario all --requests 40 --concurrency 4
    python scripts/load_test.py --json --requests 100 --concurrency 8
    python scripts/load_test.py --base-url http://127.0.0.1:5000 \\
        --cookie "session=<value>" --requests 200

This is a smoke/load harness, not a benchmark: it measures the local app and
database, not production capacity.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT))

#: Read at most this much of an error body into the report.
MAX_ERROR_BODY = 200
#: Per-request network timeout in seconds.
DEFAULT_TIMEOUT = 10.0

SCENARIOS = {
    "main": ("/health", "/api/conversations"),
    "dashboard": ("/billing/api/usage/dashboard?days=30",),
}
DEFAULT_PROFILE = {"requests": 40, "concurrency": 4}
MANUAL_PROFILE = {"requests": 200, "concurrency": 8}


def percentile(values: list[float], quantile: float) -> float | None:
    """Return a linearly interpolated percentile, or ``None`` for no samples.

    ``quantile`` is between 0 and 1. With one sample every percentile is that
    sample; otherwise the position is ``(n - 1) * quantile`` between the two
    neighbouring sorted values.
    """
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def structured_errors(samples: list[dict]) -> list[dict]:
    """Group failed samples by path, status and error code (bounded output)."""
    grouped: dict[tuple[str, int, str], dict] = {}
    for sample in samples:
        if sample.get("ok"):
            continue
        key = (
            str(sample.get("path", "")),
            int(sample.get("status") or 0),
            str(sample.get("error") or "error"),
        )
        entry = grouped.setdefault(
            key,
            {
                "path": key[0],
                "status": key[1],
                "code": key[2],
                "count": 0,
                "sample": (sample.get("body") or "")[:MAX_ERROR_BODY],
            },
        )
        entry["count"] += 1
    return [grouped[key] for key in sorted(grouped)]


def summarize(samples: list[dict]) -> dict:
    """Return latency percentiles and structured error counts for ``samples``."""
    latencies = [float(sample.get("latency_ms", 0.0)) for sample in samples]
    errors = structured_errors(samples)
    error_count = sum(entry["count"] for entry in errors)
    total = len(samples)
    return {
        "requests": total,
        "successes": total - error_count,
        "errors": {
            "count": error_count,
            "rate": round(error_count / total, 4) if total else 0.0,
            "items": errors,
        },
        "latency_ms": {
            "p50": _round(percentile(latencies, 0.50)),
            "p95": _round(percentile(latencies, 0.95)),
            "p99": _round(percentile(latencies, 0.99)),
            "max": _round(max(latencies)) if latencies else None,
            "mean": _round(statistics.fmean(latencies)) if latencies else None,
        },
        "by_path": _by_path(samples),
    }


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _by_path(samples: list[dict]) -> list[dict]:
    paths: dict[str, list[float]] = {}
    for sample in samples:
        paths.setdefault(str(sample.get("path", "")), []).append(
            float(sample.get("latency_ms", 0.0))
        )
    return [
        {
            "path": path,
            "requests": len(values),
            "p50": _round(percentile(values, 0.50)),
            "p95": _round(percentile(values, 0.95)),
            "p99": _round(percentile(values, 0.99)),
        }
        for path, values in sorted(paths.items())
    ]


def _request(base_url: str, path: str, cookie: str, timeout: float, request_id: str) -> dict:
    """Issue one GET and return a sample dict (never raises for HTTP failures)."""
    url = base_url.rstrip("/") + path
    headers = {
        "Accept": "application/json",
        "User-Agent": "aica-load-test/1",
        "X-Request-ID": request_id,
    }
    if cookie:
        headers["Cookie"] = cookie
    started = time.perf_counter()
    status = 0
    body = ""
    error = None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=timeout
        ) as response:
            status = response.status
            body = response.read(MAX_ERROR_BODY).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(MAX_ERROR_BODY).decode("utf-8", errors="replace")
        error = f"http_{status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        error = f"connection_error:{type(exc).__name__}"
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    ok = error is None and 200 <= status < 300
    return {
        "path": path,
        "status": status,
        "ok": ok,
        "latency_ms": round(elapsed_ms, 3),
        "error": error,
        "body": body if not ok else "",
    }


def run_load(
    base_url: str,
    paths: tuple[str, ...] | list[str],
    *,
    requests: int = DEFAULT_PROFILE["requests"],
    concurrency: int = DEFAULT_PROFILE["concurrency"],
    cookie: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Run ``requests`` GETs across ``paths`` with ``concurrency`` workers."""
    if not paths:
        raise ValueError("at least one path is required")
    if requests < 1:
        raise ValueError("requests must be at least 1")
    concurrency = max(1, min(concurrency, requests))

    work: queue.Queue[str] = queue.Queue()
    for index in range(requests):
        work.put(paths[index % len(paths)])

    samples: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        while True:
            try:
                path = work.get_nowait()
            except queue.Empty:
                return
            index = len(samples)  # approximate only; used for a correlation id
            sample = _request(base_url, path, cookie, timeout, f"load-{index}-{path}")
            with lock:
                samples.append(sample)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    report = summarize(samples)
    report["base_url"] = base_url
    report["concurrency"] = concurrency
    report["scenario"] = list(paths)
    report["elapsed_seconds"] = round(elapsed, 3)
    report["requests_per_second"] = round(requests / elapsed, 2) if elapsed else None
    return report


def _seed(app) -> int:
    """Create a deterministic user, workspace and small read workload."""
    from app.extensions import db
    from app.models import Conversation, Plugin, UsageRecord, User, Workspace

    with app.app_context():
        db.create_all()
        user = User(username="loadtest", email="loadtest@example.com")
        user.set_password("supersecret123")
        db.session.add(user)
        db.session.commit()

        workspace = Workspace(user_id=user.id, name="Load test workspace")
        db.session.add(workspace)
        db.session.commit()

        db.session.add_all(
            [
                Conversation(user_id=user.id, title=f"Load test conversation {index}")
                for index in range(10)
            ]
        )
        db.session.add_all(
            [
                Plugin(
                    id=f"load-plugin-{index:02d}",
                    name=f"Load plugin {index}",
                    version="1.0.0",
                    entry_point="load:Plugin",
                    capabilities=[],
                )
                for index in range(5)
            ]
        )
        now = datetime.now(UTC)
        db.session.add_all(
            [
                UsageRecord(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    source="chat",
                    provider="mock",
                    model="mock-1",
                    prompt_tokens=10,
                    completion_tokens=20,
                    total_tokens=30,
                    cost_estimate=0.001,
                    created_at=now - timedelta(days=index),
                )
                for index in range(7)
            ]
        )
        db.session.commit()
        return user.id


def _session_cookie(app, user_id: int) -> str:
    """Return a signed session cookie for the seeded user (bypasses login form)."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True
    cookie = client.get_cookie("session")
    if cookie is None:
        raise RuntimeError("could not create a load-test session cookie")
    return f"session={cookie.value}"


def _local_target():
    """Return ``(base_url, cookie, cleanup)`` for a seeded local server."""
    from werkzeug.serving import make_server

    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    tmp = tempfile.TemporaryDirectory()
    database = Path(tmp.name) / "load-test.db"
    os.environ["APP_ENV"] = "development"
    os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    os.environ.setdefault("SECRET_KEY", "load-test-secret")

    from app import create_app
    from app.extensions import db

    app = create_app("development")
    user_id = _seed(app)
    cookie = _session_cookie(app, user_id)

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    def cleanup() -> None:
        server.shutdown()
        thread.join(timeout=5)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        tmp.cleanup()

    return base_url, cookie, cleanup


def _print_human(report: dict) -> None:
    latency = report["latency_ms"]
    print(
        f"requests={report['requests']} concurrency={report['concurrency']} "
        f"errors={report['errors']['count']} "
        f"p50={latency['p50']}ms p95={latency['p95']}ms p99={latency['p99']}ms "
        f"rps={report['requests_per_second']}"
    )
    for item in report["errors"]["items"]:
        print(
            f"  error {item['code']} status={item['status']} "
            f"path={item['path']} count={item['count']}"
        )
        if item["sample"]:
            print(f"    {item['sample']}")


def _scenario_paths(name: str) -> tuple[str, ...]:
    if name == "all":
        return tuple(path for paths in SCENARIOS.values() for path in paths)
    return SCENARIOS[name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--profile", choices=["ci", "manual"], default="ci")
    parser.add_argument("--requests", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument(
        "--base-url",
        help="Run against an already-running server instead of a seeded local one.",
    )
    parser.add_argument(
        "--cookie",
        default=os.getenv("LOAD_TEST_COOKIE", ""),
        help="Cookie header for --base-url (for example 'session=...').",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--json", action="store_true", help="Print the report as JSON only.")
    args = parser.parse_args(argv)

    profile = MANUAL_PROFILE if args.profile == "manual" else DEFAULT_PROFILE
    requests = args.requests if args.requests is not None else profile["requests"]
    concurrency = args.concurrency if args.concurrency is not None else profile["concurrency"]
    paths = _scenario_paths(args.scenario)

    cleanup = None
    try:
        if args.base_url:
            if not args.cookie:
                parser.error("--base-url requires --cookie (or LOAD_TEST_COOKIE)")
            base_url, cookie = args.base_url, args.cookie
        else:
            base_url, cookie, cleanup = _local_target()
        report = run_load(
            base_url,
            paths,
            requests=requests,
            concurrency=concurrency,
            cookie=cookie,
            timeout=args.timeout,
        )
    finally:
        if cleanup is not None:
            cleanup()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report["errors"]["rate"] <= args.max_error_rate else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
