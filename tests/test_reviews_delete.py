"""Regression tests for deleting a review and its findings (#120).

Owner can delete a review; its findings cascade away; the quality metrics stop
counting it; and the review lifecycle events fire so observability/history stay
consistent.
"""

import json

from app.extensions import db
from app.models import Project, ProjectFile, Review, ReviewFinding, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY

JSON_REPLY = json.dumps(
    {
        "summary": {"overall_assessment": "ok"},
        "findings": [
            {
                "file": "app/main.py",
                "severity": "high",
                "category": "bug",
                "explanation": "e",
                "confidence": "confirmed",
            }
        ],
    }
)


class FakeProvider:
    def __init__(self, text):
        self.text = text

    def complete(self, messages, *, stream=False):
        return self.text


def _make_project(user):
    workspace = Workspace(user_id=user.id, name="Del workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Del project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    db.session.add(
        ProjectFile(
            project_id=project.id, path="app/main.py", size=10, is_binary=False, content="x"
        )
    )
    db.session.commit()
    return project


def _capture(monkeypatch):
    captured = []

    def fake(event_type, data=None, workspace_id=None, user_id=None):
        captured.append({"type": event_type, "data": data or {}, "workspace_id": workspace_id})
        return {}

    monkeypatch.setattr("app.reviews.routes.emit_event", fake)
    return captured


class TestDeleteReview:
    def test_delete_cascades_and_updates_metrics(self, client, make_user, login, monkeypatch):
        captured = _capture(monkeypatch)
        user = make_user()
        login()
        project = _make_project(user)
        review = Review(user_id=user.id, project_id=project.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        db.session.add_all(
            [
                ReviewFinding(review_id=review.id, explanation="a"),
                ReviewFinding(review_id=review.id, explanation="b"),
            ]
        )
        db.session.commit()

        before = client.get(f"/reviews/api/metrics?project_id={project.id}").get_json()
        assert before["total_reviews"] == 1
        assert before["findings"]["total"] == 2

        response = client.delete(f"/reviews/api/reviews/{review.id}")
        assert response.status_code == 200

        assert Review.query.count() == 0
        assert ReviewFinding.query.count() == 0

        after = client.get(f"/reviews/api/metrics?project_id={project.id}").get_json()
        assert after["total_reviews"] == 0
        assert after["findings"]["total"] == 0

        deleted = next(e for e in captured if e["type"] == "review.deleted")
        assert deleted["data"]["review_id"] == review.id
        assert deleted["workspace_id"] == project.workspace_id

    def test_delete_without_findings(self, client, make_user, login, monkeypatch):
        _capture(monkeypatch)
        user = make_user()
        login()
        review = Review(user_id=user.id, source="project", kind="tests")
        db.session.add(review)
        db.session.commit()
        assert client.delete(f"/reviews/api/reviews/{review.id}").status_code == 200
        assert Review.query.count() == 0

    def test_other_user_cannot_delete(self, client, make_user, login, monkeypatch):
        _capture(monkeypatch)
        other = make_user(username="other", email="other@example.com")
        make_user()
        login()
        review = Review(user_id=other.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        assert client.delete(f"/reviews/api/reviews/{review.id}").status_code == 404
        assert Review.query.count() == 1

    def test_completed_event_emitted_on_run(self, client, make_user, login, monkeypatch):
        captured = _capture(monkeypatch)
        monkeypatch.setattr("app.services.reviews.get_provider", lambda: FakeProvider(JSON_REPLY))
        user = make_user()
        login()
        project = _make_project(user)
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "quality"},
        )
        assert response.status_code == 201
        completed = [e for e in captured if e["type"] == "review.completed"]
        assert completed
        assert completed[0]["workspace_id"] == project.workspace_id

    def test_detail_page_exposes_delete_control(self, client, make_user, login):
        user = make_user()
        login()
        review = Review(user_id=user.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
        assert 'id="delete-review"' in html
