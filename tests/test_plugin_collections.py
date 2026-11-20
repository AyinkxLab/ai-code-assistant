"""Featured and curated plugin collections (#694)."""

import pytest

from app.models import Plugin, PluginCollection


def _admin(db, make_user):
    user = make_user(username="maintainer", email="maintainer@example.com")
    user.is_admin = True
    db.session.commit()
    return user


def _plugin(db, plugin_id="curated-plugin"):
    plugin = Plugin(
        id=plugin_id,
        name=f"Plugin {plugin_id}",
        version="1.0.0",
        description="A plugin used in collection tests.",
        entry_point="plugins.test:TestPlugin",
        capabilities=[],
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


def _login_admin(client, db, make_user, login):
    admin = _admin(db, make_user)
    login(email=admin.email)
    return admin


def _create(client, **overrides):
    payload = {
        "name": "Onboarding",
        "description": "Start here.",
        "featured": True,
        "plugin_ids": ["curated-plugin"],
    }
    payload.update(overrides)
    return client.post("/plugins/api/collections", json=payload)


class TestPermissionEnforcement:
    def test_non_admin_cannot_create_or_see_the_curation_ui(self, app, client, make_user, login):
        make_user()
        login()

        response = client.post(
            "/plugins/api/collections",
            json={"name": "Nope", "description": "", "plugin_ids": []},
        )
        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

        page = client.get("/plugins/collections/manage")
        assert page.status_code == 403
        assert PluginCollection.query.count() == 0

    def test_admin_can_create_and_manage(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)
        _plugin(db)

        response = _create(client)

        assert response.status_code == 201
        body = response.get_json()
        assert body["slug"] == "onboarding"
        assert body["featured"] is True
        assert body["plugins"][0]["id"] == "curated-plugin"
        assert body["plugins"][0]["available"] is True


class TestValidation:
    def test_description_is_bounded(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)

        response = _create(client, description="x" * 281)

        assert response.status_code == 400
        body = response.get_json()
        assert body["code"] == "invalid"
        assert "280" in body["error"]

    def test_plugin_count_and_id_format_are_bounded(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)

        too_many = _create(client, name="Too many", plugin_ids=[f"p{index}" for index in range(51)])
        assert too_many.status_code == 400
        assert "at most 50" in too_many.get_json()["error"]

        bad_id = _create(client, name="Bad id", plugin_ids=["has spaces"])
        assert bad_id.status_code == 400
        assert "Invalid plugin id" in bad_id.get_json()["error"]

        duplicate = _create(client, name="Duplicate", plugin_ids=["a", "a"])
        assert duplicate.status_code == 400
        assert "Duplicate" in duplicate.get_json()["error"]

    def test_slug_and_featured_are_validated(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)

        bad_slug = _create(client, slug="Not A Slug")
        assert bad_slug.status_code == 400
        assert "slug" in bad_slug.get_json()["error"]

        bad_featured = _create(client, name="Bad featured", featured="yes")
        assert bad_featured.status_code == 400
        assert "featured" in bad_featured.get_json()["error"]

    def test_slug_conflict_returns_409(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)
        assert _create(client).status_code == 201

        conflict = _create(client, name="Onboarding")

        assert conflict.status_code == 409
        assert conflict.get_json()["code"] == "conflict"


class TestCurationLifecycle:
    def test_update_delete_and_not_found_shape(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)
        created = _create(client).get_json()

        updated = client.put(
            f"/plugins/api/collections/{created['id']}",
            json={"description": "Updated copy", "featured": False, "plugin_ids": []},
        )
        assert updated.status_code == 200
        assert updated.get_json()["description"] == "Updated copy"
        assert updated.get_json()["featured"] is False

        deleted = client.delete(f"/plugins/api/collections/{created['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/plugins/api/collections/{created['slug']}").status_code == 404

        missing = client.get("/plugins/api/collections/9999")
        assert missing.status_code == 404
        assert missing.get_json()["code"] == "not_found"

    def test_featured_filter_and_public_listing(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)
        make_user()
        _create(client, name="Featured one", featured=True)
        _create(client, name="Regular one", slug="regular-one", featured=False)

        login(email="tester@example.com")

        all_items = client.get("/plugins/api/collections").get_json()
        assert all_items["count"] == 2
        assert all_items["collections"][0]["featured"] is True

        featured = client.get("/plugins/api/collections?featured=true").get_json()
        assert featured["count"] == 1
        assert featured["collections"][0]["name"] == "Featured one"


class TestRendering:
    def test_collections_page_renders_curated_metadata(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)
        _plugin(db)
        _create(client, plugin_ids=["curated-plugin", "not-registered-yet"])

        make_user()
        login(email="tester@example.com")
        response = client.get("/plugins/collections")
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "Onboarding" in html
        assert "Start here." in html
        assert "Plugin curated-plugin" in html
        assert "not available in this deployment" in html

    def test_marketplace_payload_exposes_featured_collections(
        self, app, client, db, make_user, login
    ):
        _login_admin(client, db, make_user, login)
        _create(client, name="Featured onboarding", featured=True)
        _create(client, name="Not featured", slug="not-featured", featured=False)
        make_user()
        login(email="tester@example.com")

        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 200
        featured = response.get_json()["featured_collections"]
        assert [item["name"] for item in featured] == ["Featured onboarding"]

    def test_manage_page_renders_the_create_form_for_admins(
        self, app, client, db, make_user, login
    ):
        _login_admin(client, db, make_user, login)

        response = client.get("/plugins/collections/manage")
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "Create a collection" in html
        assert 'name="description"' in html
        assert 'maxlength="280"' in html

    def test_create_form_persists_and_redirects(self, app, client, db, make_user, login):
        _login_admin(client, db, make_user, login)

        response = client.post(
            "/plugins/collections/manage",
            data={
                "name": "Security basics",
                "description": "Audit-friendly plugins.",
                "plugin_ids_text": "security-a, security-b",
                "featured": "1",
            },
        )

        assert response.status_code == 302
        collection = PluginCollection.query.filter_by(slug="security-basics").one()
        assert collection.featured is True
        assert collection.plugin_ids == ["security-a", "security-b"]


def test_oversized_metadata_is_rejected_not_truncated(app, db, make_user):
    from app.services.plugin_collections import CollectionError, create_collection

    user = _admin(db, make_user)
    with pytest.raises(CollectionError):
        create_collection(
            {"name": "A" * 200, "description": "B" * 300, "plugin_ids": []},
            actor_id=user.id,
        )
    assert PluginCollection.query.count() == 0

    collection = create_collection(
        {"name": "Security basics", "description": "B" * 280, "plugin_ids": []},
        actor_id=user.id,
    )
    assert collection.slug == "security-basics"
    assert len(collection.description) == 280
