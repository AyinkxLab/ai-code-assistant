"""Render and validate the Helm chart (#706).

Helm is not installed in the repository test environment, so this file has two
paths: when `helm` is on PATH it renders with `helm template`; otherwise a
small renderer evaluates the subset of Go-template syntax the chart uses
(scalar values, `toYaml | nindent`, `if`/`end`). Both paths assert the same
structure. The docs state which path is required in CI.
"""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "ai-code-assistant"
HELM = shutil.which("helm")

_ACTION = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}")
_IF = re.compile(r"^\{\{-?\s*if\s+([\w.]+)\s*-?\}\}$")
_END = re.compile(r"^\{\{-?\s*end\s*-?\}\}$")
_TO_YAML = re.compile(r"^\{\{-?\s*toYaml\s+([\w.]+)\s*\|\s*nindent\s+(\d+)\s*-?\}\}$")


def _lookup(context: dict[str, Any], dotted: str) -> Any:
    current: Any = context
    for part in dotted.strip(".").split("."):
        if not isinstance(current, dict) or part not in current:
            return ""
        current = current[part]
    return current


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fallback_render(template: str, context: dict[str, Any]) -> str:
    rendered: list[str] = []
    active = [True]
    for line in template.splitlines():
        stripped = line.strip()
        match = _IF.match(stripped)
        if match:
            value = _lookup(context, match.group(1))
            active.append(active[-1] and bool(value))
            continue
        if _END.match(stripped):
            if len(active) > 1:
                active.pop()
            continue
        if not active[-1]:
            continue
        match = _TO_YAML.match(stripped)
        if match:
            value = _lookup(context, match.group(1))
            dumped = yaml.safe_dump(value, sort_keys=False).rstrip()
            indent = int(match.group(2))
            rendered.extend(" " * indent + row for row in dumped.splitlines())
            continue

        def replace(action: re.Match[str]) -> str:
            return _scalar(_lookup(context, action.group(1)))

        rendered.append(_ACTION.sub(replace, line))
    return "\n".join(rendered)


def _helm_render(extra_args: list[str]) -> str:
    result = subprocess.run(
        ["helm", "template", "aica", str(CHART_DIR), "--namespace", "aica", *extra_args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _documents(*, database: bool = False) -> list[dict[str, Any]]:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    if database:
        values["database"]["enabled"] = True

    if HELM:
        rendered = (
            _helm_render(["--set", "database.enabled=true"]) if database else _helm_render([])
        )
    else:
        context = {
            "Release": {"Name": "aica", "Namespace": "aica"},
            "Chart": {"Name": "ai-code-assistant", "AppVersion": "0.1.0"},
            "Values": values,
        }
        chunks: list[str] = []
        for path in sorted((CHART_DIR / "templates").glob("*.yaml")):
            chunks.append(_fallback_render(path.read_text(encoding="utf-8"), context))
        rendered = "\n---\n".join(chunks)

    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    for document in documents:
        assert "kind" in document and "metadata" in document, document
    return documents


def _by_kind(documents: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in documents if doc["kind"] == kind]


def _container(document: dict[str, Any]) -> dict[str, Any]:
    containers = document["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def test_chart_metadata_and_values_schema():
    chart = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    assert chart["apiVersion"] == "v2"
    assert chart["type"] == "application"
    assert chart["version"] and chart["appVersion"]
    assert chart["kubeVersion"].startswith(">=")

    schema = yaml.safe_load((CHART_DIR / "values.schema.json").read_text(encoding="utf-8"))
    assert schema["required"] == ["image", "secrets", "web", "worker"]
    secrets = schema["properties"]["secrets"]
    assert secrets["required"] == ["existingSecret"]
    assert secrets["properties"]["existingSecret"]["minLength"] == 1
    web = schema["properties"]["web"]
    assert web["properties"]["replicaCount"]["minimum"] == 1
    assert web["required"] == ["replicaCount", "resources"]


def test_values_contain_no_credentials():
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert values["secrets"]["existingSecret"] == "aica-secrets"
    assert "password" not in values["database"]["auth"]
    assert "secretKey" not in values["database"]["auth"]
    text = (CHART_DIR / "values.yaml").read_text(encoding="utf-8").lower()
    assert "changeme" not in text
    assert "insecure" not in text


def test_web_and_worker_deployments_render_with_probes_limits_and_secret_refs():
    documents = _documents()
    assert len(_by_kind(documents, "Deployment")) == 2
    web = next(
        doc for doc in _by_kind(documents, "Deployment") if doc["metadata"]["name"] == "aica-web"
    )
    worker = next(
        doc for doc in _by_kind(documents, "Deployment") if doc["metadata"]["name"] == "aica-worker"
    )

    assert web["spec"]["replicas"] == 2
    assert web["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
    web_container = _container(web)
    assert web_container["image"] == "ghcr.io/ayinkxlab/ai-code-assistant:0.1.0"
    assert web_container["envFrom"] == [{"secretRef": {"name": "aica-secrets", "optional": False}}]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert web_container[probe]["httpGet"]["path"] == "/health"
    assert "limits" in web_container["resources"] and "requests" in web_container["resources"]
    assert web["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    assert web_container["securityContext"]["allowPrivilegeEscalation"] is False

    assert worker["spec"]["replicas"] == 1
    worker_container = _container(worker)
    assert worker_container["args"] == ["jobs", "worker"]
    assert "livenessProbe" in worker_container
    assert worker_container["resources"]["limits"]["memory"] == "1Gi"
    assert worker_container["envFrom"] == [
        {"secretRef": {"name": "aica-secrets", "optional": False}}
    ]


def test_service_and_ingress_route_to_the_web_pods():
    documents = _documents()
    services = _by_kind(documents, "Service")
    assert len(services) == 1
    service = services[0]
    web = next(
        doc for doc in _by_kind(documents, "Deployment") if doc["metadata"]["name"] == "aica-web"
    )
    assert service["spec"]["selector"] == web["spec"]["selector"]["matchLabels"]
    assert service["spec"]["ports"][0]["port"] == 80

    ingress = _by_kind(documents, "Ingress")[0]
    assert (
        ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    )
    assert ingress["spec"]["tls"][0]["secretName"] == "aica-tls"
    assert ingress["spec"]["rules"][0]["host"] == "assistant.example.com"
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert backend["service"]["name"] == "aica-web"


def test_migration_job_is_a_pre_upgrade_hook():
    job = _by_kind(_documents(), "Job")[0]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    container = _container(job)
    assert container["args"] == ["db", "upgrade"]
    assert container["envFrom"][0]["secretRef"]["name"] == "aica-secrets"


def test_database_is_off_by_default_and_uses_secret_keys_when_enabled():
    assert not _by_kind(_documents(), "StatefulSet")

    documents = _documents(database=True)
    statefulset = _by_kind(documents, "StatefulSet")[0]
    container = _container(statefulset)
    env = {item["name"]: item for item in container["env"]}
    assert env["POSTGRES_USER"]["valueFrom"]["secretKeyRef"] == {
        "name": "aica-database",
        "key": "username",
    }
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "password"
    assert "value" not in env["POSTGRES_PASSWORD"]
    claim = statefulset["spec"]["volumeClaimTemplates"][0]["spec"]
    assert claim["resources"]["requests"]["storage"] == "20Gi"
    assert statefulset["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    assert not _by_kind(documents, "Secret")


def test_ingress_can_be_disabled():
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    values = copy.deepcopy(values)
    values["ingress"]["enabled"] = False
    if HELM:
        pytest.skip(
            "helm path: the default-values render covers ingress; disabling is checked only offline"
        )
    context = {
        "Release": {"Name": "aica"},
        "Chart": {"Name": "ai-code-assistant"},
        "Values": values,
    }
    template = (CHART_DIR / "templates" / "ingress.yaml").read_text(encoding="utf-8")
    rendered = _fallback_render(template, context)
    assert yaml.safe_load(rendered) is None
