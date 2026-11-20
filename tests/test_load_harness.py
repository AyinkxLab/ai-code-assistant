"""Unit tests for the load-test reporting logic (#701)."""

import pytest

from scripts import load_test


def _sample(path="/health", status=200, latency=10.0, error=None, body=""):
    return {
        "path": path,
        "status": status,
        "ok": error is None and 200 <= status < 300,
        "latency_ms": latency,
        "error": error,
        "body": body,
    }


def test_percentile_handles_empty_single_and_interpolated_values():
    assert load_test.percentile([], 0.95) is None
    assert load_test.percentile([7.0], 0.95) == 7.0
    # 10 values: positions are 4.5 for p50 and 8.55 for p95.
    values = [float(value) for value in range(1, 11)]
    assert load_test.percentile(values, 0.50) == 5.5
    assert load_test.percentile(values, 0.95) == pytest.approx(9.55)
    assert load_test.percentile(values, 1.0) == 10.0


def test_percentile_rejects_an_out_of_range_quantile():
    try:
        load_test.percentile([1.0], 1.5)
    except ValueError as exc:
        assert "between 0 and 1" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected ValueError")


def test_summarize_reports_percentiles_and_error_rate():
    samples = [
        _sample(latency=10.0),
        _sample(latency=20.0),
        _sample(latency=30.0),
        _sample(latency=40.0),
        _sample(status=500, latency=50.0, error="http_500", body='{"error":"boom"}'),
    ]

    report = load_test.summarize(samples)

    assert report["requests"] == 5
    assert report["successes"] == 4
    assert report["errors"]["count"] == 1
    assert report["errors"]["rate"] == 0.2
    assert report["latency_ms"]["p50"] == 30.0
    assert report["latency_ms"]["max"] == 50.0
    assert report["latency_ms"]["mean"] == 30.0


def test_structured_errors_group_by_path_status_and_code():
    samples = [
        _sample(path="/health", status=500, error="http_500", body="first"),
        _sample(path="/health", status=500, error="http_500", body="second"),
        _sample(path="/api", status=0, error="connection_error:TimeoutError"),
        _sample(path="/health"),
    ]

    errors = load_test.structured_errors(samples)

    assert len(errors) == 2
    assert errors[0]["path"] == "/api"
    assert errors[0]["count"] == 1
    assert errors[0]["status"] == 0
    health = next(item for item in errors if item["path"] == "/health")
    assert health["count"] == 2
    assert health["status"] == 500
    assert health["sample"] == "first"


def test_summarize_is_empty_safe():
    report = load_test.summarize([])

    assert report["requests"] == 0
    assert report["successes"] == 0
    assert report["errors"]["rate"] == 0.0
    assert report["latency_ms"] == {
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
        "mean": None,
    }
    assert report["by_path"] == []


def test_scenarios_cover_the_main_api_and_dashboard_paths():
    paths = load_test._scenario_paths("all")
    assert "/health" in paths
    assert "/api/conversations" in paths
    assert any(path.startswith("/billing/api/usage/dashboard") for path in paths)
