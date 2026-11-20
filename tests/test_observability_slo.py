"""SLO, dashboard and alert definition tests (#704).

The definitions are only useful if they consume metrics the application
actually emits, so every expression is checked against the live registry.
"""

from app.observability.metrics import REGISTRY
from app.observability.slo import (
    load_definitions,
    referenced_metrics,
    validate_definitions,
)


def test_definitions_validate_against_the_emitted_metric_set():
    document = load_definitions()
    assert validate_definitions(document, REGISTRY.names()) == []


def test_every_contract_metric_is_emitted():
    document = load_definitions()
    emitted = set(REGISTRY.names())
    assert set(document["metrics_contract"]) <= emitted


def test_every_slo_has_a_target_and_a_metric_expression():
    document = load_definitions()
    emitted = set(REGISTRY.names())
    assert len(document["objectives"]) >= 3
    for objective in document["objectives"]:
        assert objective["expr"]
        assert referenced_metrics(objective["expr"], emitted)


def test_alert_rules_are_complete_and_use_bounded_metric_names():
    document = load_definitions()
    emitted = set(REGISTRY.names())
    assert set(document["metrics_contract"]) == set(emitted)
    alerts = document["alerts"]
    assert {alert["labels"]["severity"] for alert in alerts} <= {"warning", "critical"}
    for alert in alerts:
        assert alert["for"]
        assert alert["annotations"]["summary"]
        assert referenced_metrics(alert["expr"], emitted)


def test_dashboard_panels_reference_only_emitted_metrics():
    document = load_definitions()
    emitted = set(REGISTRY.names())
    panels = document["dashboard"]["panels"]
    assert panels
    for panel in panels:
        assert panel["targets"]
        for target in panel["targets"]:
            assert target["expr"]
            assert referenced_metrics(target["expr"], emitted) <= emitted


def test_validation_rejects_a_typoed_metric():
    document = load_definitions()
    broken = {
        **document,
        "alerts": [
            {
                **document["alerts"][0],
                "expr": "sum(rate(http_requests_totl[5m])) > 0.01",
            }
        ],
    }
    errors = validate_definitions(broken, REGISTRY.names())
    assert any("http_requests_totl" in error for error in errors)


def test_validation_rejects_an_alert_without_severity_or_summary():
    document = load_definitions()
    broken = {
        **document,
        "alerts": [
            {
                "alert": "Broken",
                "expr": "sum(rate(http_requests_total[5m])) > 0",
                "for": "5m",
            }
        ],
    }
    errors = validate_definitions(broken, REGISTRY.names())
    assert any("severity" in error for error in errors)
    assert any("summary" in error for error in errors)
