"""Review history list page: filters by source/kind/status (issue #117)."""

from app.extensions import db
from app.models import Review


def _review(user, **kwargs):
    defaults = {"user_id": user.id, "source": "github_pr", "kind": "pr", "status": "completed"}
    defaults.update(kwargs)
    review = Review(**defaults)
    db.session.add(review)
    db.session.commit()
    return review


class TestReviewHistoryFilters:
    def test_index_page_has_filter_controls(self, client, make_user, login):
        make_user()
        login()

        html = client.get("/reviews/").get_data(as_text=True)

        assert 'id="review-source"' in html
        assert 'id="review-kind"' in html
        assert 'id="review-status"' in html

    def test_filter_by_source(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, source="github_pr", kind="pr")
        _review(user, source="project", kind="quality")

        data = client.get("/reviews/api/reviews?source=project").get_json()

        assert len(data) == 1
        assert data[0]["source"] == "project"

    def test_filter_by_kind(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, kind="pr")
        _review(user, source="project", kind="security")

        data = client.get("/reviews/api/reviews?kind=security").get_json()

        assert [review["kind"] for review in data] == ["security"]

    def test_filter_by_status(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, status="completed")
        _review(user, source="project", kind="quality", status="failed")

        data = client.get("/reviews/api/reviews?status=failed").get_json()

        assert len(data) == 1
        assert data[0]["status"] == "failed"

    def test_combined_filters(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, source="github_pr", kind="pr", status="completed")
        _review(user, source="project", kind="quality", status="completed")
        _review(user, source="project", kind="quality", status="failed")

        data = client.get(
            "/reviews/api/reviews?source=project&kind=quality&status=completed"
        ).get_json()

        assert len(data) == 1
        assert data[0]["source"] == "project"
        assert data[0]["kind"] == "quality"
        assert data[0]["status"] == "completed"

    def test_list_exposes_required_fields(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, findings_count=3)

        data = client.get("/reviews/api/reviews").get_json()

        assert data[0]["findings_count"] == 3
        assert data[0]["kind"] == "pr"
        assert data[0]["source"] == "github_pr"
        assert data[0]["created_at"]
