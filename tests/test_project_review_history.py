"""Tests for the project-detail review history section (#124).

The project explorer renders a dedicated "Reviews" tab that lists the project's
recent reviews and links to the project review runner and the global Reviews
page. These tests assert the section is present with the expected hooks/links
and that the underlying review list API is project-scoped.
"""

from app.extensions import db
from app.models import Project, Review, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY


def _project(make_user, login):
    user = make_user(username="reviewuser", email="reviewuser@example.com")
    login(email="reviewuser@example.com")
    workspace = Workspace(user_id=user.id, name="Review workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Demo",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    return workspace, project


def test_project_page_has_review_history_section(client, app, make_user, login):
    workspace, project = _project(make_user, login)
    response = client.get(f"/workspaces/{workspace.id}/projects/{project.id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    # A Reviews tab with a container the client fills with recent reviews.
    assert 'data-tab="reviews"' in html
    assert 'id="project-reviews"' in html
    # It links to the project review runner and the global Reviews page.
    assert f"/reviews/projects/{project.id}" in html
    assert 'href="/reviews/"' in html


def test_review_history_is_project_scoped(client, app, make_user, login):
    _workspace, project = _project(make_user, login)
    review = Review(
        user_id=project.user_id,
        project_id=project.id,
        source="project",
        kind="quality",
        status="completed",
    )
    db.session.add(review)
    db.session.commit()

    data = client.get(f"/reviews/api/reviews?project_id={project.id}").get_json()
    assert [row["id"] for row in data] == [review.id]
