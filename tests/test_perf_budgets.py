"""Tests for the advisory CI performance budget (#702)."""

from scripts import perf_budget


def _budgets():
    return {
        "enforce": False,
        "scenarios": {
            "plugins_list": {"queries": 10, "latency_ms": 100},
            "workspace_audit": {"queries": 5, "latency_ms": 100},
        },
    }


def test_all_within_budget_has_no_failures():
    failures = perf_budget.evaluate(
        {"plugins_list": {"queries": 4, "latency_ms": 10}},
        _budgets(),
    )
    assert failures == []


def test_query_and_latency_regressions_are_reported():
    failures = perf_budget.evaluate(
        {"plugins_list": {"queries": 11, "latency_ms": 101}},
        _budgets(),
    )

    metrics = {(item["metric"], item["budget"]) for item in failures}
    assert metrics == {("queries", 10), ("latency_ms", 100)}


def test_unknown_scenario_is_ignored():
    assert perf_budget.evaluate({"not-benchmarked": {"queries": 999}}, _budgets()) == []


def test_advisory_mode_is_explicit_and_non_failing(monkeypatch, capsys):
    monkeypatch.setattr(
        perf_budget,
        "run_benchmark",
        lambda: {"plugins_list": {"queries": 99, "latency_ms": 1}},
    )
    monkeypatch.setattr(perf_budget, "load_budgets", lambda _path: _budgets())

    assert perf_budget.main(["--advisory"]) == 0
    assert "advisory" in capsys.readouterr().out


def test_enforce_mode_fails_on_regression(monkeypatch):
    monkeypatch.setattr(
        perf_budget,
        "run_benchmark",
        lambda: {"plugins_list": {"queries": 99, "latency_ms": 1}},
    )
    monkeypatch.setattr(perf_budget, "load_budgets", lambda _path: _budgets())

    assert perf_budget.main(["--enforce"]) == 1


def test_environment_can_enable_enforcement(monkeypatch):
    monkeypatch.setenv("PERF_BUDGETS_ENFORCE", "1")
    assert perf_budget.resolve_enforce(False, False) is True
    assert perf_budget.resolve_enforce(False, True) is False


def test_repository_budgets_cover_every_benchmarked_scenario():
    budgets = perf_budget.load_budgets()
    for scenario in perf_budget.SCENARIOS:
        entry = budgets["scenarios"][scenario]
        assert entry["queries"] > 0
        assert entry["latency_ms"] > 0
