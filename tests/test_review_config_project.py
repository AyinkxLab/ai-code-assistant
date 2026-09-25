"""Per-project review configuration (issue #116).

Complements the existing TestReviewConfig coverage with the parts the issue
names explicitly: defaults sourced from the ``REVIEW_*`` settings, focus-area
and language persistence, and rejection of invalid kinds/ranges.
"""

from app.extensions import db
from app.models import Project, ReviewConfig, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY


def _project(user):
    workspace = Workspace(user_id=user.id, name="Review config workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Config project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _url(project):
    return f"/reviews/api/projects/{project.id}/config"


class TestReviewConfigDefaults:
    def test_defaults_come_from_review_settings(self, app, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        project = _project(user)

        monkeypatch.setitem(app.config, "REVIEW_KINDS", "security")
        monkeypatch.setitem(app.config, "REVIEW_SEVERITY_THRESHOLD", "high")
        monkeypatch.setitem(app.config, "REVIEW_MAX_FILES", 7)
        monkeypatch.setitem(app.config, "REVIEW_MAX_CONTEXT_CHARS", 12345)

        payload = client.get(_url(project)).get_json()

        assert payload["kinds"] == "security"
        assert payload["severity_threshold"] == "high"
        assert payload["max_files"] == 7
        assert payload["max_context_chars"] == 12345

    def test_defaults_used_when_no_row_exists(self, app, client, make_user, login):
        user = make_user()
        login()
        project = _project(user)

        payload = client.get(_url(project)).get_json()

        assert ReviewConfig.query.filter_by(project_id=project.id).first() is None
        assert payload["enabled"] is True
        assert payload["testing_focus"] is True
        assert payload["security_focus"] is True
        assert payload["performance_focus"] is True


class TestReviewConfigUpdates:
    def test_update_persists_focus_areas_and_languages(self, client, make_user, login):
        user = make_user()
        login()
        project = _project(user)

        response = client.patch(
            _url(project),
            json={
                "testing_focus": False,
                "security_focus": True,
                "performance_focus": False,
                "languages": "python,rust",
                "enabled": False,
            },
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["testing_focus"] is False
        assert payload["performance_focus"] is False
        assert payload["languages"] == "python,rust"
        assert payload["enabled"] is False

        row = ReviewConfig.query.filter_by(project_id=project.id).first()
        assert row is not None
        assert row.testing_focus is False
        assert row.languages == "python,rust"

    def test_rejects_unknown_review_kind(self, client, make_user, login):
        user = make_user()
        login()
        project = _project(user)

        response = client.patch(_url(project), json={"kinds": "quality,not-a-kind"})

        assert response.status_code == 400

    def test_rejects_out_of_range_context(self, client, make_user, login):
        user = make_user()
        login()
        project = _project(user)

        assert client.patch(_url(project), json={"max_context_chars": 100}).status_code == 400
        assert client.patch(_url(project), json={"max_context_chars": 999999}).status_code == 400
