"""API version and deprecation header tests (#651)."""

from __future__ import annotations

from flask import make_response

from app.api.versioning import (
    API_VERSION,
    API_VERSION_HEADER,
    DEPRECATION_HEADER,
    SUNSET_HEADER,
    apply_deprecation,
)


class TestVersionHeader:
    def test_authenticated_v1_response_reports_version(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        assert response.headers[API_VERSION_HEADER] == API_VERSION == "1"

    def test_unauthenticated_v1_response_still_reports_version(self, client):
        response = client.get("/api/v1/projects")

        assert response.status_code == 401
        assert response.headers[API_VERSION_HEADER] == "1"


class TestDeprecationHeaders:
    def test_apply_deprecation_sets_standard_headers(self, app):
        sunset = "Wed, 01 Jan 2027 00:00:00 GMT"
        replacement = "/api/v2/projects"

        with app.app_context():
            response = make_response("ok")
            apply_deprecation(response, sunset=sunset, replacement=replacement)

        assert response.headers[DEPRECATION_HEADER] == "true"
        assert response.headers[SUNSET_HEADER] == sunset
        assert response.headers["Link"] == f'<{replacement}>; rel="successor-version"'

    def test_apply_deprecation_without_replacement_omits_link(self, app):
        with app.app_context():
            response = make_response("ok")
            apply_deprecation(response, sunset="Wed, 01 Jan 2027 00:00:00 GMT")

        assert response.headers[DEPRECATION_HEADER] == "true"
        assert "Link" not in response.headers
