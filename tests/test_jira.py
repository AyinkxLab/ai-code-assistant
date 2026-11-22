"""Tests for creating Jira issues from review findings (#680)."""

from __future__ import annotations

import pytest

from app.models import (
    IntegrationCredential,
    JiraIssueLink,
    Review,
    ReviewFinding,
    Workspace,
)
from app.services import jira
from app.services.crypto import decrypt_api_key

BASE_URL = "https://example.atlassian.net"
API_TOKEN = "jira-api-token-secret"


class FakeResponse:
    def __init__(self, status_code=201, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


@pytest.fixture()
def workspace(db, make_user):
    owner = make_user(username="owner", email="owner@example.com")
    row = Workspace(user_id=owner.id, name="Jira Workspace")
    db.session.add(row)
    db.session.commit()
    return row, owner


@pytest.fixture()
def finding(db, workspace):
    _ws, owner = workspace
    review = Review(user_id=owner.id, source="project", kind="quality", status="completed")
    db.session.add(review)
    db.session.flush()
    row = ReviewFinding(
        review_id=review.id,
        file="app/main.py",
        line=12,
        severity="high",
        category="bug",
        explanation="The request is not validated.",
        recommendation="Validate the payload.",
        confidence="confirmed",
    )
    db.session.add(row)
    db.session.commit()
    return row


def _configure(workspace):
    ws, owner = workspace
    jira.configure_jira(
        ws.id,
        owner,
        base_url=BASE_URL,
        email="dev@example.com",
        api_token=API_TOKEN,
        project_key="SEC",
    )


def test_credentials_are_encrypted_at_rest(workspace):
    _configure(workspace)
    ws, _owner = workspace

    credential = IntegrationCredential.query.filter_by(
        workspace_id=ws.id, integration="jira", name="api_token"
    ).one()

    assert credential.encrypted_value != API_TOKEN
    assert API_TOKEN not in credential.encrypted_value
    assert decrypt_api_key(credential.encrypted_value) == API_TOKEN
    assert jira.is_configured(ws.id) is True


def test_severity_to_priority_mapping():
    assert jira.severity_to_priority("critical") == "Highest"
    assert jira.severity_to_priority("high") == "High"
    assert jira.severity_to_priority("medium") == "Medium"
    assert jira.severity_to_priority("low") == "Low"
    assert jira.severity_to_priority("informational") == "Lowest"
    with pytest.raises(jira.JiraValidationError):
        jira.severity_to_priority("urgent")


def test_create_issue_maps_fields_and_is_idempotent(workspace, finding, monkeypatch):
    _configure(workspace)
    ws, owner = workspace
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(201, {"id": "1001", "key": "SEC-7"})

    monkeypatch.setattr(jira.requests, "post", fake_post)

    link = jira.create_issue_for_finding(ws.id, finding, user=owner)

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == f"{BASE_URL}/rest/api/3/issue"
    assert kwargs["auth"] == ("dev@example.com", API_TOKEN)
    fields = kwargs["json"]["fields"]
    assert fields["project"] == {"key": "SEC"}
    assert fields["priority"] == {"name": "High"}
    assert fields["issuetype"] == {"name": "Bug"}
    assert fields["summary"].startswith("[high]")
    assert link.external_key == "SEC-7"
    assert link.external_id == "1001"
    assert link.priority == "High"
    assert JiraIssueLink.query.count() == 1

    duplicate, created = jira.create_or_get_issue_for_finding(ws.id, finding, user=owner)
    assert created is False
    assert duplicate.id == link.id
    assert len(calls) == 1


def test_unconfigured_and_non_owner_are_structured(workspace, finding, make_user):
    ws, owner = workspace
    with pytest.raises(jira.JiraNotConfigured):
        jira.create_issue_for_finding(ws.id, finding, user=owner)

    _configure(workspace)
    outsider = make_user(username="outsider", email="outsider@example.com")
    with pytest.raises(jira.JiraPermissionError) as exc:
        jira.create_issue_for_finding(ws.id, finding, user=outsider)
    assert exc.value.code == "permission_denied"


def test_jira_delivery_error_is_structured(workspace, finding, monkeypatch):
    _configure(workspace)
    ws, owner = workspace
    monkeypatch.setattr(
        jira.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(500, {"error": "boom"}),
    )

    with pytest.raises(jira.JiraDeliveryError) as exc:
        jira.create_issue_for_finding(ws.id, finding, user=owner)
    assert exc.value.code == "delivery_failed"
    assert JiraIssueLink.query.count() == 0


def test_configure_rejects_invalid_values(workspace):
    ws, owner = workspace
    with pytest.raises(jira.JiraValidationError):
        jira.configure_jira(
            ws.id,
            owner,
            base_url="not-a-url",
            email="dev@example.com",
            api_token="token",
            project_key="SEC",
        )


def test_owner_scoped_routes_configure_and_create(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    ws = Workspace(user_id=owner.id, name="Jira Routes")
    db.session.add(ws)
    db.session.commit()
    review = Review(user_id=owner.id, source="project", kind="security", status="completed")
    db.session.add(review)
    db.session.flush()
    finding = ReviewFinding(
        review_id=review.id,
        file="app/auth.py",
        line=4,
        severity="critical",
        category="security",
        explanation="Authentication is bypassable.",
        confidence="confirmed",
    )
    db.session.add(finding)
    db.session.commit()
    monkeypatch.setattr(
        jira.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(201, {"id": "9", "key": "SEC-9"}),
    )

    configured = client.put(
        f"/integrations/api/workspaces/{ws.id}/jira",
        json={
            "base_url": BASE_URL,
            "email": "dev@example.com",
            "api_token": API_TOKEN,
            "project_key": "SEC",
        },
    )
    assert configured.status_code == 200, configured.get_json()

    created = client.post(
        f"/integrations/api/workspaces/{ws.id}/jira/findings/{finding.id}",
        json={},
    )
    assert created.status_code == 201, created.get_json()
    assert created.get_json()["created"] is True
    assert created.get_json()["issue"]["external_key"] == "SEC-9"

    again = client.post(
        f"/integrations/api/workspaces/{ws.id}/jira/findings/{finding.id}",
        json={},
    )
    assert again.status_code == 200
    assert again.get_json()["created"] is False
