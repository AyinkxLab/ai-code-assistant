"""Review history list: filters by source/kind/status (issue #117) and
pagination.

The history list used to return every review in one response, so a long history
meant an unbounded query and an oversized payload. The pagination tests pin the
paged envelope, the page-size cap, and that paging neither drops nor repeats
rows (including reviews sharing a created_at timestamp).
"""

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import Review
from app.reviews.routes import REVIEWS_PER_PAGE_DEFAULT, REVIEWS_PER_PAGE_MAX


def _review(user, **kwargs):
    defaults = {"user_id": user.id, "source": "github_pr", "kind": "pr", "status": "completed"}
    defaults.update(kwargs)
    review = Review(**defaults)
    db.session.add(review)
    db.session.commit()
    return review


def _seed(user, count):
    """Create ``count`` reviews, newest last, one hour apart."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    created = []
    for i in range(count):
        created.append(_review(user, created_at=base + timedelta(hours=i)))
    return created


class TestReviewHistoryPagination:
    def test_default_page_is_bounded(self, client, make_user, login):
        user = make_user()
        login()
        _seed(user, REVIEWS_PER_PAGE_DEFAULT + 5)

        data = client.get("/reviews/api/reviews").get_json()

        assert len(data["items"]) == REVIEWS_PER_PAGE_DEFAULT
        assert data["page"] == 1
        assert data["per_page"] == REVIEWS_PER_PAGE_DEFAULT
        assert data["total"] == REVIEWS_PER_PAGE_DEFAULT + 5
        assert data["has_next"] is True
        assert data["has_prev"] is False

    def test_pages_partition_the_history_without_gaps_or_repeats(
        self, client, make_user, login
    ):
        user = make_user()
        login()
        _seed(user, 7)

        first = client.get("/reviews/api/reviews?per_page=3&page=1").get_json()
        second = client.get("/reviews/api/reviews?per_page=3&page=2").get_json()
        third = client.get("/reviews/api/reviews?per_page=3&page=3").get_json()

        ids = [r["id"] for r in first["items"] + second["items"] + third["items"]]
        assert len(ids) == 7
        assert len(set(ids)) == 7
        assert first["total_pages"] == 3
        assert first["has_prev"] is False
        assert first["has_next"] is True
        assert second["has_prev"] is True
        assert second["has_next"] is True
        assert third["has_prev"] is True
        assert third["has_next"] is False
        assert len(third["items"]) == 1

    def test_newest_reviews_come_first(self, client, make_user, login):
        user = make_user()
        login()
        created = _seed(user, 3)

        items = client.get("/reviews/api/reviews").get_json()["items"]

        assert [r["id"] for r in items] == [c.id for c in reversed(created)]

    def test_tied_timestamps_keep_a_stable_order(self, client, make_user, login):
        """Rows sharing created_at must still page deterministically."""
        user = make_user()
        login()
        same = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
        created = [_review(user, created_at=same) for _ in range(5)]

        first = client.get("/reviews/api/reviews?per_page=2&page=1").get_json()
        second = client.get("/reviews/api/reviews?per_page=2&page=2").get_json()

        ids = [r["id"] for r in first["items"] + second["items"]]
        assert len(set(ids)) == 4
        # Newest id first within the tie.
        assert [r["id"] for r in first["items"]] == [created[4].id, created[3].id]
        assert [r["id"] for r in second["items"]] == [created[2].id, created[1].id]

    def test_per_page_is_capped(self, client, make_user, login):
        user = make_user()
        login()
        _seed(user, 1)

        data = client.get("/reviews/api/reviews?per_page=5000").get_json()

        assert data["per_page"] == REVIEWS_PER_PAGE_MAX

    def test_out_of_range_and_nonsense_paging_is_safe(self, client, make_user, login):
        user = make_user()
        login()
        _seed(user, 2)

        beyond = client.get("/reviews/api/reviews?page=99").get_json()
        assert beyond["items"] == []
        assert beyond["total"] == 2

        for query in ("page=0", "page=-3", "page=abc", "per_page=0", "per_page=-1"):
            data = client.get(f"/reviews/api/reviews?{query}").get_json()
            assert data["page"] == 1, query
            assert data["per_page"] == REVIEWS_PER_PAGE_DEFAULT, query

    def test_pagination_respects_filters(self, client, make_user, login):
        user = make_user()
        login()
        _seed(user, 4)
        _review(user, source="project", kind="quality", status="failed")

        data = client.get("/reviews/api/reviews?status=failed&per_page=1").get_json()

        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["total_pages"] == 1
        assert data["has_next"] is False

    def test_history_stays_scoped_to_the_owner(self, client, make_user, login):
        user = make_user()
        login()
        _seed(user, 2)
        make_user(username="other", email="other@example.com")
        other = make_user(username="outsider", email="outsider@example.com")
        _seed(other, 40)

        data = client.get("/reviews/api/reviews").get_json()

        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_empty_history_reports_a_single_empty_page(self, client, make_user, login):
        make_user()
        login()

        data = client.get("/reviews/api/reviews").get_json()

        assert data == {
            "items": [],
            "total": 0,
            "page": 1,
            "per_page": REVIEWS_PER_PAGE_DEFAULT,
            "total_pages": 1,
            "has_next": False,
            "has_prev": False,
        }

    def test_index_page_renders_a_pager_container(self, client, make_user, login):
        make_user()
        login()

        html = client.get("/reviews/").get_data(as_text=True)

        assert 'id="review-pager"' in html


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

        assert len(data["items"]) == 1
        assert data["items"][0]["source"] == "project"

    def test_filter_by_kind(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, kind="pr")
        _review(user, source="project", kind="security")

        data = client.get("/reviews/api/reviews?kind=security").get_json()

        assert [review["kind"] for review in data["items"]] == ["security"]

    def test_filter_by_status(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, status="completed")
        _review(user, source="project", kind="quality", status="failed")

        data = client.get("/reviews/api/reviews?status=failed").get_json()

        assert len(data["items"]) == 1
        assert data["items"][0]["status"] == "failed"

    def test_combined_filters(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, source="github_pr", kind="pr", status="completed")
        _review(user, source="project", kind="quality", status="completed")
        _review(user, source="project", kind="quality", status="failed")

        data = client.get(
            "/reviews/api/reviews?source=project&kind=quality&status=completed"
        ).get_json()

        assert len(data["items"]) == 1
        assert data["items"][0]["source"] == "project"
        assert data["items"][0]["kind"] == "quality"
        assert data["items"][0]["status"] == "completed"

    def test_list_exposes_required_fields(self, client, make_user, login):
        user = make_user()
        login()
        _review(user, findings_count=3)

        data = client.get("/reviews/api/reviews").get_json()

        assert data["items"][0]["findings_count"] == 3
        assert data["items"][0]["kind"] == "pr"
        assert data["items"][0]["source"] == "github_pr"
        assert data["items"][0]["created_at"]
