"""Public API documentation site tests (#655)."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = REPO_ROOT / "docs" / "api" / "index.html"
SPEC_PATH = REPO_ROOT / "docs" / "openapi.json"


class TestGeneratedReference:
    def test_page_has_the_required_sections(self):
        page = HTML_PATH.read_text(encoding="utf-8")

        for heading in (
            "Authentication",
            "Pagination",
            "Errors",
            "Webhook verification",
            "Endpoints",
        ):
            assert f">{heading}</h2>" in page or heading in page
        assert "invalid_input" in page
        assert "X-AICA-Signature" in page

    def test_spec_is_openapi_and_lists_public_routes(self):
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

        assert spec["openapi"].startswith("3.")
        assert "/api/conversations" in spec["paths"]
        assert spec["paths"]["/api/conversations"]["get"]["responses"]["404"]

    def test_readme_and_footer_link_to_the_reference(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        base = (REPO_ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")

        assert "docs/api/index.html" in readme
        assert "docs/openapi.json" in readme
        assert "api_docs.api_docs" in base


class TestServedReference:
    def test_route_serves_the_generated_page(self, client):
        response = client.get("/api/docs")

        assert response.status_code == 200
        assert b"Webhook verification" in response.data
        assert b"Authentication" in response.data
