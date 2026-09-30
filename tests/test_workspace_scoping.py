"""Tests for workspace scoping of conversations and prompts.

Covers the workspace CRUD surface (including the single-workspace read),
the optional ``workspace_id`` foreign key on conversations and prompts, and
the ownership guarantees that keep one account's workspaces from being visible
to another (404 rather than 403, so the routes are no existence oracle).
"""

from app.extensions import db
from app.models import Conversation, Prompt, Workspace

HEADERS = {"X-CSRFToken": "ignored"}


def _create_workspace(user_id, name="Alice workspace"):
    workspace = Workspace(user_id=user_id, name=name, description="desc")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _create_conversation(user_id, title="Hello", workspace_id=None):
    conversation = Conversation(user_id=user_id, title=title, workspace_id=workspace_id)
    db.session.add(conversation)
    db.session.commit()
    return conversation


def _create_prompt(user_id, title="Prompt", workspace_id=None):
    prompt = Prompt(
        user_id=user_id,
        title=title,
        content="body",
        workspace_id=workspace_id,
    )
    db.session.add(prompt)
    db.session.commit()
    return prompt


class TestWorkspaceCrud:
    def test_get_workspace_returns_workspace(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id, "Readable")
        response = client.get(f"/workspaces/api/workspaces/{workspace.id}")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["id"] == workspace.id
        assert payload["name"] == "Readable"
        assert payload["description"] == "desc"
        assert payload["conversation_count"] == 0

    def test_get_workspace_includes_conversation_count(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        _create_conversation(user.id, "Filed", workspace_id=workspace.id)
        _create_conversation(user.id, "Loose")
        payload = client.get(f"/workspaces/api/workspaces/{workspace.id}").get_json()
        assert payload["conversation_count"] == 1

    def test_create_list_get_update_delete_roundtrip(self, client, make_user, login):
        make_user()
        login()

        created = client.post(
            "/workspaces/api/workspaces",
            json={"name": "  Project Apollo  ", "description": "  lunar  "},
            headers=HEADERS,
        )
        assert created.status_code == 201
        workspace_id = created.get_json()["id"]

        listing = client.get("/workspaces/api/workspaces").get_json()
        assert [w["id"] for w in listing["items"]] == [workspace_id]

        fetched = client.get(f"/workspaces/api/workspaces/{workspace_id}").get_json()
        assert fetched["name"] == "Project Apollo"
        assert fetched["description"] == "lunar"

        updated = client.patch(
            f"/workspaces/api/workspaces/{workspace_id}",
            json={"name": "Project Gemini", "description": ""},
            headers=HEADERS,
        )
        assert updated.status_code == 200
        assert updated.get_json()["name"] == "Project Gemini"
        # A blank description clears the field rather than storing whitespace.
        assert updated.get_json()["description"] is None

        deleted = client.delete(f"/workspaces/api/workspaces/{workspace_id}", headers=HEADERS)
        assert deleted.status_code == 200
        assert client.get(f"/workspaces/api/workspaces/{workspace_id}").status_code == 404
        assert db.session.get(Workspace, workspace_id) is None

    def test_get_requires_login(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        client.post("/auth/logout")
        response = client.get(f"/workspaces/api/workspaces/{workspace.id}")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


class TestWorkspaceOwnershipScoping:
    def test_other_user_gets_404_on_every_method(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        workspace = _create_workspace(owner.id, "Private")
        url = f"/workspaces/api/workspaces/{workspace.id}"

        # Sanity: the owner does see it, so the 404s below are about ownership.
        assert client.get(url).status_code == 200

        client.post("/auth/logout")
        make_user(username="intruder", email="intruder@example.com")
        login(email="intruder@example.com")

        assert client.get(url).status_code == 404
        assert client.patch(url, json={"name": "stolen"}, headers=HEADERS).status_code == 404
        assert client.delete(url, headers=HEADERS).status_code == 404

        # The 404 must not leak the workspace's contents, and nothing was mutated.
        assert "Private" not in client.get(url).get_data(as_text=True)
        survivor = db.session.get(Workspace, workspace.id)
        assert survivor is not None and survivor.name == "Private"

    def test_list_excludes_other_users_workspaces(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        _create_workspace(owner.id, "Mine")
        assert client.get("/workspaces/api/workspaces").get_json()["total"] == 1
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        _create_workspace(owner.id, "Theirs")

        payload = client.get("/workspaces/api/workspaces").get_json()
        assert payload["items"] == []
        assert payload["total"] == 0


class TestConversationWorkspaceColumn:
    def test_workspace_id_is_optional(self, client, make_user, login):
        make_user()
        login()
        response = client.post("/chat/conversations", json={"title": "Unfiled"}, headers=HEADERS)
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["workspace_id"] is None
        assert db.session.get(Conversation, payload["id"]).workspace_id is None

    def test_create_conversation_in_workspace(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        response = client.post(
            "/chat/conversations",
            json={"title": "Filed", "workspace_id": workspace.id},
            headers=HEADERS,
        )
        assert response.status_code == 201
        assert response.get_json()["workspace_id"] == workspace.id

    def test_create_conversation_rejects_unowned_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _create_workspace(owner.id)
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        response = client.post(
            "/chat/conversations",
            json={"title": "Sneaky", "workspace_id": workspace.id},
            headers=HEADERS,
        )
        assert response.status_code == 404
        assert Conversation.query.filter_by(title="Sneaky").first() is None

    def test_create_conversation_rejects_malformed_workspace(self, client, make_user, login):
        make_user()
        login()
        response = client.post(
            "/chat/conversations",
            json={"title": "Bad", "workspace_id": "not-a-number"},
            headers=HEADERS,
        )
        assert response.status_code == 400

    def test_update_moves_conversation_between_workspaces(self, client, make_user, login, db):
        user = make_user()
        login()
        first = _create_workspace(user.id, "First")
        second = _create_workspace(user.id, "Second")
        conversation = _create_conversation(user.id, "Movable")

        response = client.patch(
            f"/chat/conversations/{conversation.id}",
            json={"workspace_id": first.id},
            headers=HEADERS,
        )
        assert response.get_json()["workspace_id"] == first.id

        response = client.patch(
            f"/chat/conversations/{conversation.id}",
            json={"workspace_id": second.id},
            headers=HEADERS,
        )
        assert response.get_json()["workspace_id"] == second.id

        # "all" / null unfiles the conversation back into the unscoped list.
        response = client.patch(
            f"/chat/conversations/{conversation.id}",
            json={"workspace_id": "all"},
            headers=HEADERS,
        )
        assert response.get_json()["workspace_id"] is None

    def test_update_rejects_unowned_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _create_workspace(owner.id)
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        conversation = _create_conversation(owner.id, "Target")

        response = client.patch(
            f"/chat/conversations/{conversation.id}",
            json={"workspace_id": workspace.id},
            headers=HEADERS,
        )
        assert response.status_code == 404

    def test_deleting_workspace_unfiles_rather_than_deletes_conversations(
        self, client, make_user, login
    ):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        conversation = _create_conversation(user.id, "Survivor", workspace.id)

        assert (
            client.delete(f"/workspaces/api/workspaces/{workspace.id}", headers=HEADERS).status_code
            == 200
        )
        # ON DELETE SET NULL: the conversation survives, just unscoped.
        survivor = db.session.get(Conversation, conversation.id)
        assert survivor is not None
        assert survivor.workspace_id is None


class TestConversationWorkspaceFiltering:
    def test_list_filters_to_selected_workspace(self, client, make_user, login):
        user = make_user()
        login()
        first = _create_workspace(user.id, "First")
        second = _create_workspace(user.id, "Second")
        _create_conversation(user.id, "In first", first.id)
        _create_conversation(user.id, "In second", second.id)
        _create_conversation(user.id, "Unfiled")

        listed = client.get(f"/chat/conversations?workspace_id={first.id}").get_json()
        assert [c["title"] for c in listed] == ["In first"]

        listed = client.get(f"/chat/conversations?workspace_id={second.id}").get_json()
        assert [c["title"] for c in listed] == ["In second"]

        listed = client.get("/chat/conversations?workspace_id=all").get_json()
        assert sorted(c["title"] for c in listed) == ["In first", "In second", "Unfiled"]

        listed = client.get("/chat/conversations").get_json()
        assert len(listed) == 3

    def test_workspace_filter_combines_with_search(self, client, make_user, login):
        user = make_user()
        login()
        first = _create_workspace(user.id, "First")
        second = _create_workspace(user.id, "Second")
        _create_conversation(user.id, "Deploy notes", first.id)
        _create_conversation(user.id, "Deploy runbook", second.id)

        listed = client.get(f"/chat/conversations?workspace_id={first.id}&q=deploy").get_json()
        assert [c["title"] for c in listed] == ["Deploy notes"]

    def test_filter_rejects_unowned_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _create_workspace(owner.id)
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        _create_conversation(owner.id, "Private chat")

        response = client.get(f"/chat/conversations?workspace_id={workspace.id}")
        assert response.status_code == 404
        assert "Private chat" not in response.get_data(as_text=True)

    def test_filter_rejects_malformed_workspace(self, client, make_user, login):
        make_user()
        login()
        response = client.get("/chat/conversations?workspace_id=abc")
        assert response.status_code == 400

    def test_shared_conversations_stay_out_of_the_readers_workspaces(
        self, client, make_user, login
    ):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner_workspace = _create_workspace(owner.id, "Owner project")
        conversation = _create_conversation(owner.id, "Shared file", owner_workspace.id)
        reader = make_user(username="reader", email="reader@example.com")
        share = client.post(
            f"/chat/conversations/{conversation.id}/shares",
            json={"username": "reader", "permission": "read_only"},
            headers=HEADERS,
        )
        assert share.status_code == 201

        client.post("/auth/logout")
        login(email="reader@example.com")
        reader_workspace = _create_workspace(reader.id, "Reader project")

        scoped = client.get(f"/chat/conversations?workspace_id={reader_workspace.id}").get_json()
        # The reader's own workspace must not surface the owner's filed
        # conversation - that would leak the organizer's project structure.
        assert [c["title"] for c in scoped] == []

        # It is still visible in the unscoped "all workspaces" view.
        unscoped = client.get("/chat/conversations?workspace_id=all").get_json()
        assert [c["title"] for c in unscoped] == ["Shared file"]

    def test_chat_page_scopes_sidebar_to_workspace(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id, "Sidebar scope")
        _create_conversation(user.id, "Inside", workspace.id)
        _create_conversation(user.id, "Outside")

        page = client.get(f"/chat/?workspace_id={workspace.id}")
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert "Inside" in body
        assert "Outside" not in body
        # The selector lists the workspace and marks it as the current scope.
        assert 'value="all" selected' not in body
        assert f'<option value="{workspace.id}" selected>' in body

    def test_chat_page_defaults_to_all_workspaces(self, client, make_user, login):
        user = make_user()
        login()
        _create_workspace(user.id, "Everything")
        _create_conversation(user.id, "Visible")
        body = client.get("/chat/").get_data(as_text=True)
        assert "Visible" in body
        assert 'value="all" selected' in body
        assert "Everything" in body

    def test_chat_page_404s_for_unowned_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _create_workspace(owner.id)
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        response = client.get(f"/chat/?workspace_id={workspace.id}")
        assert response.status_code == 404
        assert "Nothing here" not in response.get_data(as_text=True)

    def test_sidebar_selector_markup_is_rendered(self, client, make_user, login):
        make_user()
        login()
        body = client.get("/chat/").get_data(as_text=True)
        assert 'id="workspace-select"' in body
        assert 'id="new-workspace"' in body
        assert "All workspaces" in body

    def test_chat_js_scopes_the_sidebar(self, client):
        script = client.get("/static/js/chat.js")
        assert script.status_code == 200
        source = script.get_data(as_text=True)
        # The list request carries the selected workspace...
        assert 'params.set("workspace_id", selectedWorkspaceId())' in source
        # ...new conversations are filed under it...
        assert "payload.workspace_id = selectedWorkspaceId();" in source
        # ...and the selector can create one inline.
        assert "/workspaces/api/workspaces" in source
        assert 'getElementById("workspace-select")' in source
        assert 'getElementById("new-workspace")' in source


class TestPromptWorkspaceScoping:
    def test_create_prompt_in_workspace(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        response = client.post(
            "/prompts/api/prompts",
            json={"title": "T", "content": "C", "workspace_id": workspace.id},
            headers=HEADERS,
        )
        assert response.status_code == 201
        assert response.get_json()["workspace_id"] == workspace.id

    def test_create_prompt_rejects_unowned_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _create_workspace(owner.id)
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        response = client.post(
            "/prompts/api/prompts",
            json={"title": "T", "content": "C", "workspace_id": workspace.id},
            headers=HEADERS,
        )
        assert response.status_code == 404
        assert Prompt.query.filter_by(title="T").first() is None

    def test_update_prompt_moves_it(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        prompt = _create_prompt(user.id, "Movable")
        response = client.patch(
            f"/prompts/api/prompts/{prompt.id}",
            json={"workspace_id": workspace.id},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.get_json()["workspace_id"] == workspace.id

    def test_update_prompt_rejects_unowned_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _create_workspace(owner.id)
        prompt = _create_prompt(owner.id, "Guarded")
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        # Not the caller's prompt: 404 before the workspace is even considered.
        response = client.patch(
            f"/prompts/api/prompts/{prompt.id}",
            json={"workspace_id": workspace.id},
            headers=HEADERS,
        )
        assert response.status_code == 404

    def test_list_filters_prompts_by_workspace(self, client, make_user, login):
        user = make_user()
        login()
        first = _create_workspace(user.id, "First")
        second = _create_workspace(user.id, "Second")
        _create_prompt(user.id, "In first", first.id)
        _create_prompt(user.id, "In second", second.id)
        _create_prompt(user.id, "Unfiled")

        listed = client.get(f"/prompts/api/prompts?workspace_id={first.id}").get_json()
        assert [p["title"] for p in listed] == ["In first"]

        listed = client.get(f"/prompts/api/prompts?workspace_id={second.id}").get_json()
        assert [p["title"] for p in listed] == ["In second"]

        listed = client.get("/prompts/api/prompts").get_json()
        assert len(listed) == 3

    def test_list_rejects_unowned_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _create_workspace(owner.id)
        _create_prompt(owner.id, "Secret prompt")
        client.post("/auth/logout")

        make_user(username="other", email="other@example.com")
        login(email="other@example.com")
        response = client.get(f"/prompts/api/prompts?workspace_id={workspace.id}")
        assert response.status_code == 404
        assert "Secret prompt" not in response.get_data(as_text=True)

    def test_team_prompt_workspace_cannot_be_changed(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        response = client.post(
            "/prompts/api/prompts",
            json={
                "title": "Team",
                "content": "C",
                "is_team": True,
                "workspace_id": workspace.id,
            },
            headers=HEADERS,
        )
        assert response.status_code == 201
        prompt_id = response.get_json()["id"]
        moved = client.patch(
            f"/prompts/api/prompts/{prompt_id}",
            json={"workspace_id": None},
            headers=HEADERS,
        )
        assert moved.status_code == 400
