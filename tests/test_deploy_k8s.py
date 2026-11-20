"""Structural checks for the plain Kubernetes manifests (#707).

The manifests are parsed as YAML and asserted against the deployment contract:
probes, resource limits, secret references, hardened pod security, and no
committed credentials. There is no cluster in this environment, so nothing here
applies the manifests or asks the API server to validate them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = REPO_ROOT / "deploy" / "k8s"
DOCS = REPO_ROOT / "docs" / "kubernetes.md"
SECRET_NAME = "aica-secrets"


def _documents() -> dict[tuple[str, str], dict[str, Any]]:
    documents: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(K8S_DIR.glob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict), f"{path.name} is not a YAML mapping"
        documents[(loaded["kind"], loaded["metadata"]["name"])] = loaded
    return documents


def _deployment(name: str) -> dict[str, Any]:
    return _documents()[("Deployment", name)]


def _container(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _walk(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_every_manifest_has_api_version_kind_and_name():
    documents = _documents()
    assert len(documents) == 6, sorted(documents)
    for (kind, name), document in documents.items():
        assert document["apiVersion"].startswith(
            ("v1", "apps/", "batch/", "networking.k8s.io/", "kustomize.config.k8s.io/")
        ), kind
        assert kind and name


def test_web_deployment_is_hardened_and_probed():
    deployment = _deployment("aica-web")
    assert deployment["spec"]["replicas"] >= 2
    rolling = deployment["spec"]["strategy"]["rollingUpdate"]
    assert rolling == {"maxUnavailable": 0, "maxSurge": 1}

    pod_spec = deployment["spec"]["template"]["spec"]
    pod_security = pod_spec["securityContext"]
    assert pod_security["runAsNonRoot"] is True
    assert pod_security["seccompProfile"]["type"] == "RuntimeDefault"

    container = _container(deployment)
    assert container["image"].startswith("ghcr.io/ayinkxlab/ai-code-assistant:")
    assert not container["image"].endswith(":latest")
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert probe in container, probe
        assert container[probe]["httpGet"]["path"] == "/health"
        assert container[probe]["httpGet"]["port"] == "http"
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["requests"]["cpu"]
    assert container["resources"]["requests"]["memory"]
    assert container["resources"]["limits"]["cpu"]
    assert container["resources"]["limits"]["memory"]
    assert container["envFrom"] == [{"secretRef": {"name": SECRET_NAME, "optional": False}}]


def test_worker_runs_the_job_queue_and_shares_the_secret():
    deployment = _deployment("aica-worker")
    container = _container(deployment)
    assert container["command"] == ["flask"]
    assert container["args"] == ["jobs", "worker"]
    assert "livenessProbe" in container
    assert container["resources"]["limits"]["memory"] == "1Gi"
    assert container["envFrom"] == [{"secretRef": {"name": SECRET_NAME, "optional": False}}]
    assert deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True


def test_service_selects_the_web_pods():
    documents = _documents()
    service = documents[("Service", "aica-web")]
    web = _deployment("aica-web")
    assert service["spec"]["selector"] == web["spec"]["selector"]["matchLabels"]
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}
    ]


def test_ingress_exposes_the_service_with_tls_and_sse_settings():
    ingress = _documents()[("Ingress", "aica-web")]
    annotations = ingress["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    assert ingress["spec"]["tls"][0]["secretName"] == "aica-tls"
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert backend["service"]["name"] == "aica-web"
    assert backend["service"]["port"]["name"] == "http"


def test_migration_job_is_bounded_and_uses_the_shared_secret():
    job = _documents()[("Job", "aica-migrate")]
    assert job["spec"]["backoffLimit"] == 3
    assert job["spec"]["activeDeadlineSeconds"] > 0
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    container = pod_spec["containers"][0]
    assert container["command"] == ["flask"]
    assert container["args"] == ["db", "upgrade"]
    assert container["envFrom"] == [{"secretRef": {"name": SECRET_NAME, "optional": False}}]


def test_manifests_contain_no_default_credentials_or_secret_objects():
    documents = _documents()
    assert not any(kind == "Secret" for kind, _ in documents)
    for (kind, name), document in documents.items():
        for key, value in _walk(document):
            assert key.lower() not in {"password", "secret_key", "database_url"}, (kind, name, key)
            if isinstance(value, str):
                lowered = value.lower()
                assert "insecure" not in lowered, (kind, name, value)
                assert "changeme" not in lowered, (kind, name, value)


def test_kustomization_lists_every_manifest():
    kustomization = yaml.safe_load((K8S_DIR / "kustomization.yaml").read_text(encoding="utf-8"))
    assert kustomization["namespace"] == "aica"
    listed = set(kustomization["resources"])
    present = {path.name for path in K8S_DIR.glob("*.yaml")} - {"kustomization.yaml"}
    assert listed == present


def test_docs_cover_secrets_storage_and_deploy_permissions():
    text = DOCS.read_text(encoding="utf-8")
    for required in (
        SECRET_NAME,
        "SECRET_KEY",
        "DATABASE_URL",
        "aica-tls",
        "PostgreSQL",
        "storage",
        "kubectl apply -k",
        "RBAC",
    ):
        assert required in text, required
