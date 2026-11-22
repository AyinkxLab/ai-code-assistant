"""Tests for the acknowledgement ticketing integration (#630)."""

from __future__ import annotations

import json

import pytest

from app.extensions import db
from app.models import (
    IntegrationCredential,
    Project,
    Review,
    ReviewFinding,
    SecurityTicketLink,
    Workspace,
)
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.models.review import SOURCE_PROJECT, STATUS_COMPLETED
from app.services import security_tickets
from app.services.crypto import decrypt_api_key


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakePost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected provider call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _setup(make_user, *, severity="high", addressed=False):
    user = make_user()
    workspace = Workspace(user_id=user.id, name="Org")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    review = Review(
        user_id=user.id,
        project_id=project.id,
        source=SOURCE_PROJECT,
        kind="security",
        status=STATUS_COMPLETED,
    )
    db.session.add(review)
    db.session.commit()
    finding = ReviewFinding(
        review_id=review.id,
        file="app/db.py",
        line=12,
        severity=severity,
        category="injection",
        explanation="Unparameterized query.",
        recommendation="Use bound parameters.",
        confidence="confirmed",
        addressed=addressed,
    )
    db.session.add(finding)
    db.session.commit()
    return user, workspace, finding


def _configure_jira(workspace, owner):
    return security_tickets.configure_ticketing(
        workspace.id,
        owner,
        provider="jira",
        base_url="https://acme.atlassian.net",
        email="security@acme.test",
        api_token="jira-secret-token",
        project_key="SEC",
    )


def _configure_linear(workspace, owner):
    return security_tickets.configure_ticketing(
        workspace.id,
        owner,
        provider="linear",
        api_token="lin-secret-token",
        team_id="team-123",
    )


class TestConfiguration:
    def test_owner_stores_encrypted_credentials(self, db, make_user):
        owner, workspace, _ = _setup(make_user)

        result = _configure_jira(workspace, owner)

        assert result["provider"] == "jira"
        assert result["configured"] is True
        assert set(result["credential_names"]) == {
            "base_url",
            "email",
            "api_token",
            "project_key",
        }
        stored = IntegrationCredential.query.filter_by(
            workspace_id=workspace.id,
            integration="ticketing",
            name="api_token",
        ).one()
        assert stored.encrypted_value != "jira-secret-token"
        assert decrypt_api_key(stored.encrypted_value) == "jira-secret-token"
        assert security_tickets.is_configured(workspace.id) is True

    def test_non_owner_cannot_configure(self, db, make_user):
        _owner, workspace, _ = _setup(make_user)
        outsider = make_user(username="outsider", email="outsider@example.com")

        with pytest.raises(security_tickets.TicketingPermissionError) as info:
            _configure_jira(workspace, outsider)

        assert info.value.code == "permission_denied"
        assert info.value.http_status == 403
        assert security_tickets.is_configured(workspace.id) is False

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"provider": "service-desk", "api_token": "x", "team_id": "t"},
            {"provider": "jira", "base_url": "ftp://x", "email": "a@b.c"},
            {
                "provider": "jira",
                "base_url": "https://user:pass@x.test",
                "email": "a@b.c",
                "api_token": "t",
                "project_key": "SEC",
            },
            {
                "provider": "jira",
                "base_url": "https://x.test",
                "email": "not-an-email",
                "api_token": "t",
                "project_key": "SEC",
            },
            {
                "provider": "linear",
                "api_token": "",
                "team_id": "team-1",
            },
        ],
    )
    def test_invalid_configuration_is_structured(self, db, make_user, kwargs):
        owner, workspace, _ = _setup(make_user)

        with pytest.raises(security_tickets.TicketingValidationError) as info:
            security_tickets.configure_ticketing(workspace.id, owner, **kwargs)

        assert info.value.code == "invalid_input"


class TestTicketCreation:
    def test_jira_ticket_is_created_once_and_retries_are_idempotent(self, db, make_user):
        owner, workspace, finding = _setup(make_user, addressed=True)
        _configure_jira(workspace, owner)
        post = FakePost(FakeResponse(201, {"key": "SEC-42", "id": "10042"}))

        first = security_tickets.create_for_acknowledged_finding(finding, post=post)
        second = security_tickets.create_for_acknowledged_finding(finding, post=post)

        assert first.id == second.id
        assert first.external_key == "SEC-42"
        assert first.external_url == "https://acme.atlassian.net/browse/SEC-42"
        assert SecurityTicketLink.query.count() == 1
        assert len(post.calls) == 1

        call = post.calls[0]
        assert call["url"] == "https://acme.atlassian.net/rest/api/3/issue"
        assert call["auth"] == ("security@acme.test", "jira-secret-token")
        fields = call["json"]["fields"]
        assert fields["project"] == {"key": "SEC"}
        assert fields["priority"] == {"name": "High"}
        assert "Unparameterized query." in fields["description"]
        assert "jira-secret-token" not in json.dumps(call["json"])

    def test_linear_ticket_payload(self, db, make_user):
        owner, workspace, finding = _setup(make_user, addressed=True)
        _configure_linear(workspace, owner)
        post = FakePost(
            FakeResponse(
                200,
                {
                    "data": {
                        "issueCreate": {
                            "success": True,
                            "issue": {
                                "identifier": "ENG-7",
                                "url": "https://linear.app/acme/issue/ENG-7",
                            },
                        }
                    }
                },
            )
        )

        link = security_tickets.create_for_acknowledged_finding(finding, post=post)

        assert link.provider == "linear"
        assert link.external_key == "ENG-7"
        assert post.calls[0]["url"] == security_tickets.LINEAR_ENDPOINT
        assert post.calls[0]["headers"]["Authorization"] == "lin-secret-token"
        issue_input = post.calls[0]["json"]["variables"]["input"]
        assert issue_input["teamId"] == "team-123"
        assert issue_input["priority"] == 2

    def test_misses_are_noops(self, db, make_user):
        owner, workspace, finding = _setup(make_user, addressed=False)
        _configure_jira(workspace, owner)
        post = FakePost()

        assert security_tickets.create_for_acknowledged_finding(finding, post=post) is None

        finding.addressed = True
        finding.severity = "low"
        db.session.commit()
        assert security_tickets.create_for_acknowledged_finding(finding, post=post) is None
        assert post.calls == []

    def test_unconfigured_workspace_is_a_noop(self, db, make_user):
        _owner, _workspace, finding = _setup(make_user, addressed=True)
        post = FakePost()

        assert security_tickets.create_for_acknowledged_finding(finding, post=post) is None
        assert post.calls == []
        assert SecurityTicketLink.query.count() == 0

    def test_payload_is_bounded(self, db, make_user):
        owner, workspace, finding = _setup(make_user, addressed=True)
        finding.explanation = "x" * 20_000
        db.session.commit()
        _configure_jira(workspace, owner)
        post = FakePost(FakeResponse(201, {"key": "SEC-1"}))

        security_tickets.create_for_acknowledged_finding(finding, post=post)

        fields = post.calls[0]["json"]["fields"]
        assert len(fields["summary"]) <= security_tickets.MAX_TITLE
        assert len(fields["description"]) <= security_tickets.MAX_DESCRIPTION
        encoded = json.dumps(post.calls[0]["json"]).encode("utf-8")
        assert len(encoded) <= security_tickets.MAX_REQUEST_BYTES

    def test_delivery_failure_is_structured_and_records_nothing(self, db, make_user):
        owner, workspace, finding = _setup(make_user, addressed=True)
        _configure_jira(workspace, owner)
        post = FakePost(FakeResponse(500, {"error": "boom"}))

        with pytest.raises(security_tickets.TicketingDeliveryError) as info:
            security_tickets.create_for_acknowledged_finding(finding, post=post)

        assert info.value.code == "delivery_failed"
        assert info.value.http_status == 502
        assert SecurityTicketLink.query.count() == 0


class TestAcknowledgementRoute:
    def _finding(self, make_user):
        user, workspace, finding = _setup(make_user)
        return user, workspace, finding

    def test_acknowledging_a_high_risk_finding_creates_a_ticket(
        self, db, client, make_user, login, monkeypatch
    ):
        owner, workspace, finding = self._finding(make_user)
        _configure_jira(workspace, owner)
        post = FakePost(FakeResponse(201, {"key": "SEC-9"}))
        monkeypatch.setattr(security_tickets.requests, "post", post)
        login(email=owner.email)

        response = client.patch(
            f"/reviews/api/reviews/findings/{finding.id}",
            json={"addressed": True},
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["addressed"] is True
        assert payload["ticket"]["external_key"] == "SEC-9"
        assert len(post.calls) == 1

        # Re-acknowledging (or a retried request) reuses the stored link.
        again = client.patch(
            f"/reviews/api/reviews/findings/{finding.id}",
            json={"addressed": True},
        )
        assert again.get_json()["ticket"]["external_key"] == "SEC-9"
        assert len(post.calls) == 1

    def test_low_risk_or_plain_updates_do_not_create_tickets(
        self, db, client, make_user, login, monkeypatch
    ):
        owner, workspace, finding = self._finding(make_user)
        finding.severity = "low"
        db.session.commit()
        _configure_jira(workspace, owner)
        post = FakePost()
        monkeypatch.setattr(security_tickets.requests, "post", post)
        login(email=owner.email)

        response = client.patch(
            f"/reviews/api/reviews/findings/{finding.id}",
            json={"addressed": True},
        )

        assert response.status_code == 200
        assert "ticket" not in response.get_json()
        assert post.calls == []

    def test_delivery_failure_rolls_back_the_acknowledgement(
        self, db, client, make_user, login, monkeypatch
    ):
        owner, workspace, finding = self._finding(make_user)
        _configure_jira(workspace, owner)
        monkeypatch.setattr(
            security_tickets.requests,
            "post",
            FakePost(FakeResponse(503, {"error": "unavailable"})),
        )
        login(email=owner.email)

        response = client.patch(
            f"/reviews/api/reviews/findings/{finding.id}",
            json={"addressed": True},
        )

        assert response.status_code == 502
        assert response.get_json()["code"] == "bad_gateway"
        db.session.expire_all()
        assert db.session.get(ReviewFinding, finding.id).addressed is False
        assert SecurityTicketLink.query.count() == 0
