"""Public API v1 resource tests (#646)."""

from __future__ import annotations

import json
from pathlib import Path

from app.extensions import db
from app.models import Project, Review, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.models.review import SOURCE_PROJECT, STATUS_COMPLETED
from app.services import usage_ledger


def _workspace(user, name="Main"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _project(user, workspace, name="Project"):
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name=name,
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _review(user, project, source=SOURCE_PROJECT, kind="quality"):
    review = Review(
        user_id=user.id,
        project_id=project.id,
        source=source,
        kind=kind,
        status=STATUS_COMPLETED,
    )
    db.session.add(review)
    db.session.commit()
    return review


class TestPublishedSpec:
    def test_openapi_reference_lists_every_v1_resource(self):
        spec_path = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))

        for path in (
            "/api/v1/projects",
            "/api/v1/projects/{project_id}",
            "/api/v1/reviews",
            "/api/v1/reviews/{review_id}",
            "/api/v1/usage",
        ):
            assert path in spec["paths"]


class TestAuth:
    def test_every_v1_resource_requires_authentication(self, client):
        for path in (
            "/api/v1/projects",
            "/api/v1/projects/1",
            "/api/v1/reviews",
            "/api/v1/reviews/1",
            "/api/v1/usage",
        ):
            response = client.get(path)
            assert response.status_code == 401
            assert response.get_json()["code"] == "unauthorized"


class TestProjects:
    def test_list_is_owner_scoped_and_paginated(self, client, make_user, login):
        user = make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        login(email="owner@example.com")
        workspace = _workspace(user)
        other_workspace = _workspace(other, "Other")
        first = _project(user, workspace, "First")
        second = _project(user, workspace, "Second")
        third = _project(user, workspace, "Third")
        _project(other, other_workspace, "Not mine")

        response = client.get("/api/v1/projects?page=1&limit=2")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["total"] == 3
        assert payload["page"] == 1
        assert payload["limit"] == 2
        assert [item["id"] for item in payload["items"]] == [third.id, second.id]
        assert payload["next_cursor"]

        next_page = client.get(f"/api/v1/projects?limit=2&cursor={payload['next_cursor']}")
        assert [item["id"] for item in next_page.get_json()["items"]] == [first.id]
        assert next_page.get_json()["next_cursor"] is None

    def test_list_filters_by_owned_workspace(self, client, make_user, login):
        user = make_user()
        other = make_user(username="other", email="other@example.com")
        login()
        workspace = _workspace(user)
        other_workspace = _workspace(other, "Other")
        _project(user, workspace, "Mine")
        _project(other, other_workspace, "Theirs")

        response = client.get(f"/api/v1/projects?workspace_id={workspace.id}")
        assert [item["name"] for item in response.get_json()["items"]] == ["Mine"]

        hidden = client.get(f"/api/v1/projects?workspace_id={other_workspace.id}")
        assert hidden.get_json()["items"] == []

    def test_detail_returns_owned_project_and_404_for_others(self, client, make_user, login):
        user = make_user()
        other = make_user(username="other", email="other@example.com")
        login()
        workspace = _workspace(user)
        other_workspace = _workspace(other, "Other")
        project = _project(user, workspace)
        foreign = _project(other, other_workspace, "Foreign")

        mine = client.get(f"/api/v1/projects/{project.id}")
        assert mine.status_code == 200
        payload = mine.get_json()
        assert payload["id"] == project.id
        assert payload["workspace_id"] == workspace.id
        assert "status" in payload and "file_count" in payload

        denied = client.get(f"/api/v1/projects/{foreign.id}")
        assert denied.status_code == 404
        assert denied.get_json()["code"] == "not_found"

    def test_invalid_workspace_filter_is_structured(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/api/v1/projects?workspace_id=nope")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_malformed_cursor_is_structured(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/api/v1/projects?cursor=not-a-cursor")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"


class TestReviews:
    def test_list_is_owner_scoped_with_filters(self, client, make_user, login):
        user = make_user()
        other = make_user(username="other", email="other@example.com")
        login()
        workspace = _workspace(user)
        other_workspace = _workspace(other, "Other")
        project = _project(user, workspace)
        quality = _review(user, project, kind="quality")
        _review(user, project, kind="security")
        _review(other, _project(other, other_workspace, "Foreign"), kind="quality")

        response = client.get("/api/v1/reviews?kind=quality")
        payload = response.get_json()

        assert response.status_code == 200
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == quality.id

    def test_detail_returns_owned_review_and_404_for_others(self, client, make_user, login):
        user = make_user()
        other = make_user(username="other", email="other@example.com")
        login()
        project = _project(user, _workspace(user))
        review = _review(user, project)
        foreign = _review(other, _project(other, _workspace(other, "Other")))

        response = client.get(f"/api/v1/reviews/{review.id}")
        assert response.status_code == 200
        assert response.get_json()["id"] == review.id
        assert "findings_count" in response.get_json()

        denied = client.get(f"/api/v1/reviews/{foreign.id}")
        assert denied.status_code == 404
        assert denied.get_json()["code"] == "not_found"

    def test_invalid_project_filter_is_structured(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/api/v1/reviews?project_id=abc")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"


class TestUsage:
    def test_usage_is_scoped_to_the_caller(self, client, make_user, login):
        user = make_user()
        other = make_user(username="other", email="other@example.com")
        login()
        usage_ledger.record_usage(source="chat", user=user, prompt_tokens=10, total_tokens=10)
        usage_ledger.record_usage(source="review", user=user, prompt_tokens=20, total_tokens=20)
        usage_ledger.record_usage(source="chat", user=other, prompt_tokens=999, total_tokens=999)

        response = client.get("/api/v1/usage?page=1&limit=1&source=chat")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["schema_version"] == usage_ledger.EXPORT_SCHEMA_VERSION
        assert payload["pagination"]["total"] == 1
        assert payload["filters"]["source"] == "chat"
        assert len(payload["records"]) == 1
        assert payload["records"][0]["user_id"] == user.id
        assert payload["totals"]["total_tokens"] == 10

    def test_usage_rejects_malformed_pagination(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/api/v1/usage?page=nope")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_usage_cursor_is_not_silently_ignored(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/api/v1/usage?cursor=eyJpZCI6MX0")

        assert response.status_code == 400
        assert "Cursor" in response.get_json()["error"]
