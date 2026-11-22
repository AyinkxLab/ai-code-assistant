"""Tests for Linear issue creation from findings (#681)."""

from __future__ import annotations

import pytest

from app.services import linear_issues


class FakeClient:
    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.created: list[dict] = []

    def find_issue(self, idempotency_key: str) -> dict | None:
        return self.issues.get(idempotency_key)

    def create_issue(self, *, title, description, priority, idempotency_key):
        issue = {
            "identifier": f"ENG-{len(self.created) + 1}",
            "url": f"https://linear.example/issue/{len(self.created) + 1}",
        }
        self.issues[idempotency_key] = issue
        self.created.append(
            {
                "title": title,
                "description": description,
                "priority": priority,
                "idempotency_key": idempotency_key,
            }
        )
        return issue


def test_maps_severity_and_includes_backlink():
    client = FakeClient()
    issue = linear_issues.create_issue_from_finding(
        client=client,
        project_id=7,
        finding_id="finding-1",
        title="SQL injection in search",
        severity="high",
        description="Unsanitized input reaches the query.",
        backlink="https://app.example/reviews/1#finding-1",
        file="app/search.py",
        line=42,
    )
    assert issue.created
    assert issue.identifier == "ENG-1"
    request = client.created[0]
    assert request["priority"] == 2
    assert "Unsanitized input" in request["description"]
    assert "app/search.py:42" in request["description"]
    assert "https://app.example/reviews/1#finding-1" in request["description"]


def test_is_idempotent_for_the_same_finding():
    client = FakeClient()
    first = linear_issues.create_issue_from_finding(
        client=client,
        project_id=7,
        finding_id="finding-1",
        title="A",
        severity="low",
    )
    second = linear_issues.create_issue_from_finding(
        client=client,
        project_id=7,
        finding_id="finding-1",
        title="A",
        severity="low",
    )
    assert first.created
    assert not second.created
    assert second.identifier == first.identifier
    assert len(client.created) == 1


def test_rejects_unknown_severity_and_invalid_input():
    client = FakeClient()
    with pytest.raises(linear_issues.LinearError) as excinfo:
        linear_issues.create_issue_from_finding(
            client=client,
            project_id=1,
            finding_id="f",
            title="t",
            severity="catastrophic",
        )
    assert excinfo.value.code == "invalid_severity"

    with pytest.raises(linear_issues.LinearError) as excinfo:
        linear_issues.create_issue_from_finding(
            client=client, project_id=1, finding_id="", title="t", severity="high"
        )
    assert excinfo.value.code == "invalid_finding"


def test_transport_failure_is_structured():
    class BrokenClient(FakeClient):
        def create_issue(self, **kwargs):
            raise RuntimeError("network down")

    with pytest.raises(linear_issues.LinearError) as excinfo:
        linear_issues.create_issue_from_finding(
            client=BrokenClient(),
            project_id=1,
            finding_id="f",
            title="t",
            severity="high",
        )
    assert excinfo.value.code == "unavailable"
    assert "network down" in str(excinfo.value)
