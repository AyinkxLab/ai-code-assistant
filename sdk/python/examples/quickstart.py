"""Runnable Python SDK quickstart (#664).

Demonstrates authentication, listing projects with pagination, a structured
error, and webhook delivery logs. It runs against the bundled local mock server::

    python sdk/python/examples/mock_server.py

or against any API by setting ``AICA_BASE_URL`` and ``AICA_API_TOKEN``::

    python sdk/python/examples/quickstart.py
"""

from __future__ import annotations

import os

from ai_code_assistant import Client, NotFoundError

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TOKEN = "demo-token"


def run(base_url: str = DEFAULT_BASE_URL, api_token: str = DEFAULT_TOKEN) -> dict:
    """Exercise the SDK and return a small JSON-friendly summary."""
    client = Client(base_url, api_token=api_token, max_retries=1)

    page_one = client.list_projects(page=1, per_page=2)
    page_two = client.list_projects(page=2, per_page=2)

    try:
        client.get_project(9999)
    except NotFoundError as exc:
        error = {"type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
    else:  # pragma: no cover - the mock server always returns 404 for id 9999
        error = None

    # Delivery logs live under the webhooks blueprint (#672); use the generic
    # request helper until a typed webhook model lands.
    deliveries = client.request(
        "GET",
        "/webhooks/api/subscriptions/1/deliveries",
        params={"status": "failure", "per_page": 5},
    )

    return {
        "projects_page_1": [project.name for project in page_one],
        "projects_page_2": [project.name for project in page_two],
        "error": error,
        "webhook_deliveries": deliveries.data.get("items", []),
    }


def main(base_url: str | None = None, api_token: str | None = None) -> dict:
    """Run the quickstart and print a short summary."""
    summary = run(
        base_url or os.getenv("AICA_BASE_URL", DEFAULT_BASE_URL),
        api_token or os.getenv("AICA_API_TOKEN", DEFAULT_TOKEN),
    )
    print("Projects (page 1):", summary["projects_page_1"])
    print("Projects (page 2):", summary["projects_page_2"])
    print("Structured error:", summary["error"])
    print("Webhook deliveries:", len(summary["webhook_deliveries"]))
    return summary


if __name__ == "__main__":
    main()
