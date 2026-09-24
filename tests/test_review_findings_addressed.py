"""Regression tests for toggling review findings as addressed (#119).

The ``addressed`` flag persists on the ``ReviewFinding`` row, is owner-scoped,
drives the findings filter, and feeds the quality metrics
(``addressed`` / ``unaddressed_high_risk``).
"""

from app.extensions import db
from app.models import Project, Review, ReviewFinding, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY


def _review_with_findings(make_user, login, username="addruser", email="addruser@example.com"):
    user = make_user(username=username, email=email)
    login(email=email)
    workspace = Workspace(user_id=user.id, name="Addressed workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Addressed project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    review = Review(user_id=user.id, project_id=project.id, source="project", kind="quality")
    db.session.add(review)
    db.session.commit()

    high = ReviewFinding(
        review_id=review.id,
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        explanation="high severity",
        confidence="confirmed",
    )
    low = ReviewFinding(
        review_id=review.id,
        file="b.py",
        line=2,
        severity="low",
        category="other",
        explanation="low severity",
        confidence="suggestion",
    )
    db.session.add_all([high, low])
    db.session.commit()
    return {
        "user": user,
        "project": project,
        "review": review,
        "high": high,
        "low": low,
    }


class TestToggleAddressed:
    def test_toggle_persists_both_ways(self, client, make_user, login):
        ctx = _review_with_findings(make_user, login)

        response = client.patch(
            f"/reviews/api/reviews/findings/{ctx['high'].id}", json={"addressed": True}
        )
        assert response.status_code == 200
        assert response.get_json()["addressed"] is True
        assert db.session.get(ReviewFinding, ctx["high"].id).addressed is True

        # Persisted: the stored row and a fresh read both reflect it.
        listed = client.get(
            f"/reviews/api/reviews/{ctx['review'].id}/findings?addressed=1"
        ).get_json()
        assert ctx["high"].id in [f["id"] for f in listed]

        back = client.patch(
            f"/reviews/api/reviews/findings/{ctx['high'].id}", json={"addressed": False}
        )
        assert back.get_json()["addressed"] is False
        assert db.session.get(ReviewFinding, ctx["high"].id).addressed is False

    def test_addressed_filter_separates_open_and_addressed(self, client, make_user, login):
        ctx = _review_with_findings(make_user, login)
        client.patch(f"/reviews/api/reviews/findings/{ctx['low'].id}", json={"addressed": True})

        addressed = client.get(
            f"/reviews/api/reviews/{ctx['review'].id}/findings?addressed=1"
        ).get_json()
        assert [f["id"] for f in addressed] == [ctx["low"].id]

        open_findings = client.get(
            f"/reviews/api/reviews/{ctx['review'].id}/findings?addressed=0"
        ).get_json()
        assert [f["id"] for f in open_findings] == [ctx["high"].id]

    def test_owner_scoping(self, client, make_user, login):
        ctx = _review_with_findings(make_user, login, "owner", "owner@example.com")
        finding_id = ctx["high"].id

        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")

        response = client.patch(
            f"/reviews/api/reviews/findings/{finding_id}", json={"addressed": True}
        )
        assert response.status_code == 404
        assert db.session.get(ReviewFinding, finding_id).addressed is False

    def test_metrics_reflect_addressed_state(self, client, make_user, login):
        ctx = _review_with_findings(make_user, login)

        before = client.get(f"/reviews/api/metrics?project_id={ctx['project'].id}").get_json()
        assert before["findings"]["addressed"] == 0
        assert before["findings"]["unaddressed_high_risk"] == 1

        client.patch(f"/reviews/api/reviews/findings/{ctx['high'].id}", json={"addressed": True})

        after = client.get(f"/reviews/api/metrics?project_id={ctx['project'].id}").get_json()
        assert after["findings"]["addressed"] == 1
        assert after["findings"]["unaddressed_high_risk"] == 0

    def test_unknown_finding_is_404(self, client, make_user, login):
        _review_with_findings(make_user, login)
        response = client.patch("/reviews/api/reviews/findings/999999", json={"addressed": True})
        assert response.status_code == 404

    def test_requires_login(self, client):
        response = client.patch("/reviews/api/reviews/findings/1", json={"addressed": True})
        assert response.status_code == 302
