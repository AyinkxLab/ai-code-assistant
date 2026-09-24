"""Tests for the reviews API routes: running, listing, findings, config, metrics."""

import json

from app.extensions import db
from app.models import (
    Project,
    ProjectFile,
    Review,
    ReviewConfig,
    ReviewFinding,
    Workspace,
)
from app.models.project import SOURCE_ARCHIVE, STATUS_INDEXING, STATUS_READY

JSON_REPLY = json.dumps(
    {
        "summary": {"overall_assessment": "Looks fine."},
        "findings": [
            {
                "file": "app/main.py",
                "line": 2,
                "severity": "high",
                "category": "bug",
                "explanation": "Unchecked input.",
                "recommendation": "Validate it.",
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


class FakeGithubClient:
    def __init__(self, pr, files):
        self.pr = pr
        self.files = files

    def get_pull_request(self, full_name, number):
        return self.pr

    def list_pull_request_files(self, full_name, number):
        return self.files


def _make_project(user, status=STATUS_READY, with_files=True):
    workspace = Workspace(user_id=user.id, name="API workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="API project",
        source=SOURCE_ARCHIVE,
        status=status,
    )
    db.session.add(project)
    db.session.commit()
    if with_files:
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path="app/main.py",
                size=20,
                is_binary=False,
                content="def run():\n    pass\n",
            )
        )
        db.session.commit()
    return project


def _pr_dict():
    return {
        "number": 7,
        "title": "Add feature",
        "body": "Adds a thing.",
        "state": "open",
        "merged": False,
        "user": {"login": "octocat"},
        "head": {"ref": "feature"},
        "base": {"ref": "main"},
    }


def _pr_files():
    return [
        {
            "filename": "app.py",
            "patch": "@@\n+def f()",
            "status": "modified",
            "additions": 2,
            "deletions": 0,
        }
    ]


class TestPagesRequireLogin:
    def test_index_redirects(self, client):
        assert client.get("/reviews/").status_code == 302

    def test_detail_redirects(self, client):
        assert client.get("/reviews/1").status_code == 302

    def test_project_page_redirects(self, client):
        assert client.get("/reviews/projects/1").status_code == 302

    def test_config_page_redirects(self, client):
        assert client.get("/reviews/projects/1/config").status_code == 302


class TestProjectReviews:
    def test_run_project_review(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        project = _make_project(user)
        monkeypatch.setattr("app.services.reviews.get_provider", lambda: FakeProvider(JSON_REPLY))
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "quality"},
        )
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["status"] == "completed"
        assert payload["kind"] == "quality"
        assert payload["findings_count"] == 1
        review = db.session.get(Review, payload["id"])
        assert review.summary_dict["overall_assessment"] == "Looks fine."
        assert ReviewFinding.query.filter_by(review_id=review.id).count() == 1

    def test_run_project_review_not_ready(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user, status=STATUS_INDEXING, with_files=False)
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "security"},
        )
        assert response.status_code == 409

    def test_run_project_review_disabled(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        project = _make_project(user)
        db.session.add(ReviewConfig(user_id=user.id, project_id=project.id, enabled=False))
        db.session.commit()
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "quality"},
        )
        assert response.status_code == 400

    def test_run_project_review_kind_not_enabled(self, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        project = _make_project(user)
        db.session.add(ReviewConfig(user_id=user.id, project_id=project.id, kinds="quality"))
        db.session.commit()
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "security"},
        )
        assert response.status_code == 400
        assert "not enabled" in response.get_json()["error"]

    def test_run_project_review_unsupported_kind(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "bogus"},
        )
        assert response.status_code == 400

    def test_run_project_review_provider_failure(self, client, make_user, login, monkeypatch):
        from app.services.llm import LLMProviderError

        def boom(*args, **kwargs):
            raise LLMProviderError("provider down")

        user = make_user()
        login()
        project = _make_project(user)
        monkeypatch.setattr("app.services.reviews.get_provider", boom)
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "project", "project_id": project.id, "kind": "tests"},
        )
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["status"] == "failed"
        assert payload["error_message"]


class TestPullRequestReviews:
    def test_run_pr_review(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr(
            "app.reviews.routes.get_github_client",
            lambda: FakeGithubClient(_pr_dict(), _pr_files()),
        )
        monkeypatch.setattr("app.services.reviews.get_provider", lambda: FakeProvider(JSON_REPLY))
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "github_pr", "repo": "octocat/hello", "pr_number": 7},
        )
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["status"] == "completed"
        assert payload["owner"] == "octocat"
        assert payload["repo"] == "hello"
        assert payload["pr_title"] == "Add feature"
        assert payload["base_ref"] == "main"
        assert payload["head_ref"] == "feature"

    def test_run_pr_review_invalid_repo(self, client, make_user, login):
        make_user()
        login()
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "github_pr", "repo": "not-a-repo", "pr_number": 1},
        )
        assert response.status_code == 400

    def test_run_pr_review_requires_number(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr(
            "app.reviews.routes.get_github_client",
            lambda: FakeGithubClient(_pr_dict(), _pr_files()),
        )
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "github_pr", "repo": "octocat/hello"},
        )
        assert response.status_code == 400

    def test_run_pr_review_no_connection(self, client, make_user, login, monkeypatch):
        from app.services.github import GitHubNotConnectedError

        def not_connected(*args, **kwargs):
            raise GitHubNotConnectedError("Connect your GitHub account.")

        make_user()
        login()
        monkeypatch.setattr("app.reviews.routes.get_github_client", not_connected)
        response = client.post(
            "/reviews/api/reviews",
            json={"source": "github_pr", "repo": "octocat/hello", "pr_number": 1},
        )
        assert response.status_code == 400


class TestListDetailDelete:
    def test_list_reviews(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        db.session.add(
            Review(user_id=user.id, project_id=project.id, source="project", kind="quality")
        )
        db.session.add(
            Review(user_id=user.id, source="github_pr", kind="pr", owner="o", repo="r", pr_number=1)
        )
        db.session.commit()
        response = client.get("/reviews/api/reviews")
        assert response.status_code == 200
        assert len(response.get_json()) == 2

        response = client.get("/reviews/api/reviews?kind=pr")
        assert len(response.get_json()) == 1

        response = client.get(f"/reviews/api/reviews?project_id={project.id}")
        assert len(response.get_json()) == 1

    def test_list_scoped_to_owner(self, client, make_user, login):
        other = make_user(username="other", email="other@example.com")
        make_user()
        login()
        db.session.add(Review(user_id=other.id, source="project", kind="security"))
        db.session.commit()
        response = client.get("/reviews/api/reviews")
        assert response.get_json() == []

    def test_detail_and_findings(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        review = Review(
            user_id=user.id,
            project_id=project.id,
            source="project",
            kind="quality",
            summary=json.dumps({"overall_assessment": "ok"}),
        )
        db.session.add(review)
        db.session.commit()
        db.session.add(
            ReviewFinding(
                review_id=review.id,
                file="a.py",
                severity="high",
                category="bug",
                explanation="e",
                confidence="confirmed",
            )
        )
        db.session.add(
            ReviewFinding(
                review_id=review.id,
                file="b.py",
                severity="low",
                category="other",
                explanation="x",
                confidence="suggestion",
                addressed=True,
            )
        )
        db.session.commit()

        response = client.get(f"/reviews/api/reviews/{review.id}")
        assert response.status_code == 200
        assert response.get_json()["summary"]["overall_assessment"] == "ok"

        response = client.get(f"/reviews/api/reviews/{review.id}/findings?severity=high")
        assert len(response.get_json()) == 1

        response = client.get(f"/reviews/api/reviews/{review.id}/findings?addressed=1")
        assert len(response.get_json()) == 1

        # Findings carry a [CONFIRMED]/[SUGGESTION] label derived from confidence.
        findings = client.get(f"/reviews/api/reviews/{review.id}/findings").get_json()
        labels = {f["confidence"]: f["confidence_label"] for f in findings}
        assert labels["confirmed"] == "[CONFIRMED]"
        assert labels["suggestion"] == "[SUGGESTION]"
        assert response.get_json()[0]["file"] == "b.py"

    def test_detail_exposes_categories_and_filters_by_category(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        review = Review(
            user_id=user.id,
            project_id=project.id,
            source="project",
            kind="quality",
            status="completed",
        )
        db.session.add(review)
        db.session.commit()
        db.session.add_all(
            [
                ReviewFinding(
                    review_id=review.id,
                    severity="medium",
                    category="readability",
                    explanation="e",
                    confidence="suggestion",
                ),
                ReviewFinding(
                    review_id=review.id,
                    severity="medium",
                    category="dead-code",
                    explanation="e",
                    confidence="confirmed",
                ),
            ]
        )
        db.session.commit()

        detail = client.get(f"/reviews/api/reviews/{review.id}").get_json()
        assert "readability" in detail["categories"]
        assert "dead-code" in detail["categories"]

        all_findings = client.get(f"/reviews/api/reviews/{review.id}/findings").get_json()
        assert len(all_findings) == 2

        filtered = client.get(
            f"/reviews/api/reviews/{review.id}/findings?category=readability"
        ).get_json()
        assert len(filtered) == 1
        assert filtered[0]["category"] == "readability"

    def test_detail_forbidden_for_other_user(self, client, make_user, login):
        other = make_user(username="other", email="other@example.com")
        make_user()
        login()
        review = Review(user_id=other.id, source="project", kind="tests")
        db.session.add(review)
        db.session.commit()
        assert client.get(f"/reviews/api/reviews/{review.id}").status_code == 404
        assert client.delete(f"/reviews/api/reviews/{review.id}").status_code == 404

    def test_delete_review(self, client, make_user, login):
        user = make_user()
        login()
        review = Review(user_id=user.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        db.session.add(ReviewFinding(review_id=review.id, explanation="e"))
        db.session.commit()
        response = client.delete(f"/reviews/api/reviews/{review.id}")
        assert response.status_code == 200
        assert Review.query.count() == 0
        assert ReviewFinding.query.count() == 0

    def test_toggle_finding_addressed(self, client, make_user, login):
        user = make_user()
        login()
        review = Review(user_id=user.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        finding = ReviewFinding(review_id=review.id, explanation="e")
        db.session.add(finding)
        db.session.commit()
        response = client.patch(
            f"/reviews/api/reviews/findings/{finding.id}", json={"addressed": True}
        )
        assert response.status_code == 200
        assert response.get_json()["addressed"] is True

    def test_toggle_finding_forbidden_for_other_user(self, client, make_user, login):
        other = make_user(username="other", email="other@example.com")
        make_user()
        login()
        review = Review(user_id=other.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        finding = ReviewFinding(review_id=review.id, explanation="e")
        db.session.add(finding)
        db.session.commit()
        response = client.patch(
            f"/reviews/api/reviews/findings/{finding.id}", json={"addressed": True}
        )
        assert response.status_code == 404


class TestReviewConfig:
    def test_get_config_defaults(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        response = client.get(f"/reviews/api/projects/{project.id}/config")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["enabled"] is True
        assert "quality" in payload["kinds"]

    def test_update_config(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        response = client.patch(
            f"/reviews/api/projects/{project.id}/config",
            json={"severity_threshold": "high", "max_files": 8, "kinds": "security,tests"},
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["severity_threshold"] == "high"
        assert payload["max_files"] == 8
        assert payload["kinds"] == "security,tests"

    def test_update_config_invalid_severity(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        response = client.patch(
            f"/reviews/api/projects/{project.id}/config",
            json={"severity_threshold": "catastrophic"},
        )
        assert response.status_code == 400

    def test_update_config_invalid_max_files(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        response = client.patch(
            f"/reviews/api/projects/{project.id}/config",
            json={"max_files": 0},
        )
        assert response.status_code == 400

    def test_update_config_other_user_forbidden(self, client, make_user, login):
        other = make_user(username="other", email="other@example.com")
        make_user()
        login()
        project = _make_project(other)
        response = client.get(f"/reviews/api/projects/{project.id}/config")
        assert response.status_code == 404


class TestMetrics:
    def test_user_metrics(self, client, make_user, login):
        user = make_user()
        login()
        review = Review(user_id=user.id, source="project", kind="security", status="completed")
        db.session.add(review)
        db.session.commit()
        db.session.add(
            ReviewFinding(
                review_id=review.id,
                severity="critical",
                category="injection",
                explanation="e",
                confidence="confirmed",
            )
        )
        db.session.commit()
        response = client.get("/reviews/api/metrics")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["total_reviews"] == 1
        assert payload["findings"]["total"] == 1
        assert payload["findings"]["high_risk"] == 1
        assert payload["findings"]["unaddressed_high_risk"] == 1

    def test_project_metrics(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        db.session.add(
            Review(user_id=user.id, project_id=project.id, source="project", kind="quality")
        )
        db.session.commit()
        response = client.get(f"/reviews/api/metrics?project_id={project.id}")
        assert response.status_code == 200
        assert response.get_json()["total_reviews"] == 1

    def test_workspace_metrics_aggregate(self, client, make_user, login):
        user = make_user()
        login()
        project = _make_project(user)
        review = Review(user_id=user.id, project_id=project.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        db.session.add(
            ReviewFinding(
                review_id=review.id,
                severity="high",
                category="duplication",
                explanation="e",
                confidence="confirmed",
            )
        )
        db.session.commit()

        response = client.get(f"/reviews/api/metrics?workspace_id={project.workspace_id}")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["total_reviews"] == 1
        assert payload["findings"]["total"] == 1
        assert payload["findings"]["by_severity"]["high"] == 1
        assert payload["findings"]["by_category"]["duplication"] == 1
        assert "reviews_last_7_days" in payload

    def test_workspace_metrics_owner_scoped(self, client, make_user, login):
        other = make_user(username="other", email="other@example.com")
        project = _make_project(other)
        make_user()
        login()
        response = client.get(f"/reviews/api/metrics?workspace_id={project.workspace_id}")
        assert response.status_code == 404

    def test_findings_trend_tracks_open_and_addressed(self, client, make_user, login):
        user = make_user()
        login()
        first = Review(user_id=user.id, source="project", kind="quality", status="completed")
        db.session.add(first)
        db.session.commit()
        second = Review(user_id=user.id, source="project", kind="security", status="completed")
        db.session.add(second)
        db.session.commit()
        db.session.add_all(
            [
                ReviewFinding(
                    review_id=first.id,
                    severity="high",
                    category="duplication",
                    explanation="e",
                    confidence="confirmed",
                    addressed=True,
                ),
                ReviewFinding(
                    review_id=first.id,
                    severity="medium",
                    category="readability",
                    explanation="e",
                    confidence="potential",
                    addressed=False,
                ),
                ReviewFinding(
                    review_id=second.id,
                    severity="critical",
                    category="injection",
                    explanation="e",
                    confidence="confirmed",
                    addressed=False,
                ),
            ]
        )
        db.session.commit()

        trend = client.get("/reviews/api/metrics").get_json()["findings_trend"]
        assert [point["review_id"] for point in trend] == [first.id, second.id]
        assert trend[0]["kind"] == "quality"
        assert trend[0]["open"] == 1
        assert trend[0]["addressed"] == 1
        assert trend[0]["total"] == 2
        assert trend[1]["open"] == 1
        assert trend[1]["addressed"] == 0
        assert trend[1]["total"] == 1

    def test_findings_trend_empty_without_reviews(self, client, make_user, login):
        make_user()
        login()
        assert client.get("/reviews/api/metrics").get_json()["findings_trend"] == []
