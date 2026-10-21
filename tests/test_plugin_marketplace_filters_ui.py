"""Marketplace browse filter controls (#416)."""

from app.extensions import db
from app.models import Workspace


def _workspace(owner, name="Filters WS"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def test_page_includes_search_and_capability_filters(client, make_user, login):
    owner = make_user()
    login()
    _workspace(owner)

    response = client.get("/plugins/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="plugin-catalog-search"' in html
    assert 'id="plugin-catalog-capability"' in html
    assert 'type="search"' in html
    assert "Name, author, id or capability" in html
