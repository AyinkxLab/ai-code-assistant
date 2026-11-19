"""Marketplace ratings and reviews (#692)."""

from app.extensions import db
from app.models import Plugin, PluginInstallation, PluginReview, Workspace


def _plugin(plugin_id="reviewed-plugin", author="Acme Labs"):
    plugin = Plugin(
        id=plugin_id,
        name="Reviewed Plugin",
        version="1.0.0",
        description="test",
        author=author,
        entry_point="plugins.test:TestPlugin",
        capabilities=["PROJECT_READ"],
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


def _install(plugin, owner, enabled=True):
    workspace = Workspace(user_id=owner.id, name="Review WS")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(
        PluginInstallation(
            plugin_id=plugin.id,
            workspace_id=workspace.id,
            enabled=enabled,
            installed_by_id=owner.id,
        )
    )
    db.session.commit()
    return workspace


class TestCreateReview:
    def test_install_is_required(self, app, client, make_user, login):
        make_user()
        login()
        _plugin()

        response = client.post(
            "/plugins/api/marketplace/reviewed-plugin/reviews",
            json={"rating": 5, "body": "Great"},
        )

        assert response.status_code == 403
        assert response.get_json()["code"] == "install_required"

    def test_one_review_per_user_per_plugin(self, app, client, make_user, login):
        owner = make_user()
        login()
        plugin = _plugin()
        _install(plugin, owner)

        first = client.post(
            "/plugins/api/marketplace/reviewed-plugin/reviews",
            json={"rating": 5, "title": "Great", "body": "Works"},
        )
        second = client.post(
            "/plugins/api/marketplace/reviewed-plugin/reviews",
            json={"rating": 4},
        )

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.get_json()["code"] == "duplicate_review"
        assert PluginReview.query.count() == 1

    def test_invalid_rating_is_a_structured_error(self, app, client, make_user, login):
        owner = make_user()
        login()
        plugin = _plugin()
        _install(plugin, owner)

        response = client.post(
            "/plugins/api/marketplace/reviewed-plugin/reviews",
            json={"rating": 9},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid"

    def test_list_exposes_average_and_distribution(self, app, client, make_user, login):
        owner = make_user()
        login()
        plugin = _plugin()
        _install(plugin, owner)
        client.post(
            "/plugins/api/marketplace/reviewed-plugin/reviews",
            json={"rating": 5},
        )

        payload = client.get("/plugins/api/marketplace/reviewed-plugin/reviews").get_json()

        assert payload["summary"]["count"] == 1
        assert payload["summary"]["average"] == 5.0
        assert payload["summary"]["distribution"]["5"] == 1
        assert payload["reviews"][0]["username"] == "tester"

    def test_empty_reviews_are_a_normal_empty_result(self, app, client, make_user, login):
        make_user()
        login()
        _plugin()

        payload = client.get("/plugins/api/marketplace/reviewed-plugin/reviews").get_json()

        assert payload["reviews"] == []
        assert payload["summary"]["average"] is None

    def test_unknown_plugin_is_404(self, app, client, make_user, login):
        make_user()
        login()

        response = client.get("/plugins/api/marketplace/nope/reviews")

        assert response.status_code == 404


class TestReRatingAndModeration:
    def _review(
        self, app, client, make_user, login, *, username="tester", email="tester@example.com"
    ):
        owner = make_user(username=username, email=email)
        login(email=email)
        plugin = _plugin()
        _install(plugin, owner)
        created = client.post(
            "/plugins/api/marketplace/reviewed-plugin/reviews",
            json={"rating": 3, "body": "Okay"},
        )
        return created.get_json()["id"]

    def test_author_can_rerate_and_others_cannot(self, app, client, make_user, login):
        review_id = self._review(app, client, make_user, login)

        updated = client.put(
            f"/plugins/api/marketplace/reviews/{review_id}",
            json={"rating": 5, "body": "Much better"},
        )

        assert updated.status_code == 200
        assert updated.get_json()["rating"] == 5
        assert updated.get_json()["body"] == "Much better"

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        forbidden = client.put(
            f"/plugins/api/marketplace/reviews/{review_id}",
            json={"rating": 1},
        )
        assert forbidden.status_code == 403
        assert forbidden.get_json()["code"] == "forbidden"

    def test_report_is_once_with_a_reason(self, app, client, make_user, login):
        review_id = self._review(app, client, make_user, login)

        first = client.post(
            f"/plugins/api/marketplace/reviews/{review_id}/report",
            json={"reason": "spam"},
        )
        second = client.post(
            f"/plugins/api/marketplace/reviews/{review_id}/report",
            json={"reason": "spam again"},
        )
        missing = client.post(f"/plugins/api/marketplace/reviews/{review_id}/report", json={})

        assert first.status_code == 200
        assert first.get_json()["reported"] is True
        assert second.status_code == 409
        assert second.get_json()["code"] == "already_reported"
        assert missing.status_code == 409

    def test_publisher_can_respond_but_unrelated_user_cannot(self, app, client, make_user, login):
        review_id = self._review(app, client, make_user, login)

        make_user(username="outsider", email="outsider@example.com")
        login(email="outsider@example.com")
        forbidden = client.post(
            f"/plugins/api/marketplace/reviews/{review_id}/response",
            json={"response": "Thanks"},
        )
        assert forbidden.status_code == 403
        assert forbidden.get_json()["code"] == "forbidden"

        make_user(username="acme-labs", email="publisher@example.com")
        login(email="publisher@example.com")
        allowed = client.post(
            f"/plugins/api/marketplace/reviews/{review_id}/response",
            json={"response": "Thanks for the feedback"},
        )

        assert allowed.status_code == 200
        assert allowed.get_json()["owner_response"] == "Thanks for the feedback"


class TestReviewsPage:
    def test_page_renders_reviews_and_form(self, app, client, make_user, login):
        owner = make_user()
        login()
        plugin = _plugin()
        _install(plugin, owner)
        client.post(
            "/plugins/api/marketplace/reviewed-plugin/reviews",
            json={"rating": 4, "title": "Solid", "body": "Works well"},
        )

        response = client.get("/plugins/marketplace/reviewed-plugin/reviews")
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "Solid" in html
        assert "4 / 5" in html
        assert 'id="review-rating"' in html
        assert "Submit review" in html

    def test_page_has_an_empty_state(self, app, client, make_user, login):
        make_user()
        login()
        _plugin()

        response = client.get("/plugins/marketplace/reviewed-plugin/reviews")

        assert response.status_code == 200
        assert "No reviews yet" in response.get_data(as_text=True)

    def test_page_requires_authentication(self, client):
        response = client.get("/plugins/marketplace/reviewed-plugin/reviews")
        assert response.status_code in (302, 401)
