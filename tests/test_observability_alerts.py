"""Alert rules, runbook links and the metrics they depend on (#705).

The rules are only useful if every expression references a metric the
application actually emits and the runbook anchor exists. Both are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.observability.metrics import REGISTRY
from app.observability.slo import load_definitions, referenced_metrics
from app.services import job_queue

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "deploy" / "monitoring" / "alert-rules.yaml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "incident-runbook.md"
SLO_ALERTS = {
    "AicaAvailabilityFastBurn",
    "AicaAvailabilitySlowBurn",
    "AicaHighErrorRate",
    "AicaErrorBurstByEndpoint",
    "AicaLlmLatencyP95High",
}


def _rules() -> list[dict]:
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    rules: list[dict] = []
    for group in document["groups"]:
        assert group["name"] and group["interval"]
        rules.extend(group["rules"])
    return rules


def _normalize(expression: str) -> str:
    return re.sub(r"\s+", " ", expression).strip()


def test_rule_names_are_unique_and_cover_every_required_signal():
    rules = _rules()
    names = [rule["alert"] for rule in rules]
    assert len(names) == len(set(names))
    expected = {
        "AicaAvailabilityFastBurn",
        "AicaAvailabilitySlowBurn",
        "AicaHighErrorRate",
        "AicaErrorBurstByEndpoint",
        "AicaLlmLatencyP95High",
        "AicaJobQueueBacklog",
        "AicaWebhookDeliveryFailures",
    }
    assert expected <= set(names)


def test_every_rule_has_labels_annotations_and_a_real_runbook_anchor():
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    anchors = set(re.findall(r"^##\s+(.+)$", runbook, re.MULTILINE))
    for rule in _rules():
        assert rule["for"], rule["alert"]
        labels = rule["labels"]
        assert labels["severity"] in {"warning", "critical"}, rule["alert"]
        assert labels["component"], rule["alert"]
        if rule["alert"] in SLO_ALERTS:
            assert labels["slo"] in {"availability", "error_rate", "llm_latency_p95"}, rule["alert"]
        annotations = rule["annotations"]
        assert annotations["summary"] and annotations["description"], rule["alert"]
        target = annotations["runbook"]
        path, _, anchor = target.partition("#")
        assert (REPO_ROOT / path).resolve() == RUNBOOK_PATH.resolve(), target
        assert anchor, target
        assert any(anchor in _slug(title) for title in anchors), target


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def test_expressions_reference_only_emitted_metrics():
    emitted = set(REGISTRY.names())
    assert {"job_queue_depth", "webhook_deliveries_total"} <= emitted
    for rule in _rules():
        metrics = referenced_metrics(rule["expr"], emitted)
        assert metrics, rule["alert"]
        assert metrics <= emitted, (rule["alert"], sorted(metrics - emitted))


def test_shared_rules_match_the_slo_definitions():
    defined = {alert["alert"]: alert for alert in load_definitions()["alerts"]}
    for rule in _rules():
        if rule["alert"] in defined:
            assert _normalize(rule["expr"]) == _normalize(defined[rule["alert"]]["expr"])
            assert rule["labels"]["severity"] == defined[rule["alert"]]["labels"]["severity"]
            assert rule["for"] == defined[rule["alert"]]["for"]


def test_runbook_covers_every_alert_family():
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    for heading in ("Triage", "Rollback", "Escalation"):
        if heading == "Triage":  # the section is titled "First five minutes"
            assert "First five minutes" in text
        else:
            assert f"## {heading}" in text
    for alert in _rules():
        assert alert["alert"] in text, alert["alert"]


def test_queue_depth_gauge_tracks_jobs(app, db):
    REGISTRY.reset()
    try:
        job_queue.enqueue("scan.project", {"project_id": 404})
        job_queue.refresh_queue_metrics()
        rendered = REGISTRY.render()
        assert 'job_queue_depth{status="queued"} 1' in rendered
        assert 'job_queue_depth{status="succeeded"} 0' in rendered
    finally:
        REGISTRY.reset()


def test_webhook_counter_records_success_and_failure(monkeypatch):
    REGISTRY.reset()

    class _Response:
        status_code = 202

    monkeypatch.setattr(job_queue, "validate_webhook_url", lambda url: url)
    monkeypatch.setattr(job_queue.requests, "post", lambda *args, **kwargs: _Response())
    assert job_queue.deliver_webhook({"url": "https://example.com", "payload": {}}) == {
        "status_code": 202
    }
    assert 'webhook_deliveries_total{result="success"} 1' in REGISTRY.render()

    def _fail(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(job_queue.requests, "post", _fail)
    with pytest.raises(OSError):
        job_queue.deliver_webhook({"url": "https://example.com", "payload": {}})
    assert 'webhook_deliveries_total{result="failure"} 1' in REGISTRY.render()
    REGISTRY.reset()
