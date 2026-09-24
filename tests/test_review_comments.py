"""Tests for inline code-review comments (#52).

Covers thread CRUD anchored to assistant messages (code block / line range),
replies, the resolve/unresolve toggle with owner/author permissions, @mention
notifications, and the conversation review summary panel.
"""

from app.extensions import db
from app.models import (
    Notification,
    Project,
    ProjectMessage,
    ReviewComment,
    User,
    Workspace,
    WorkspaceMember,
)
from app.models.review_comment import REVIEW_COMMENT_MAX_LENGTH

MESSAGE_BODY = (
    "Here is the fix:\n\n"
    "```python\nprint('one')\n```\n\n"
    "And a second block:\n\n"
    "```js\nconsole.log('two')\n```\n"
)


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _setup(owner, member_emails=()):
    workspace = Workspace(user_id=owner.id, name="Review comments workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=owner.id,
        name="Commented source",
        source="archive",
        status="ready",
    )
    db.session.add(project)
    db.session.commit()
    for member in member_emails:
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="viewer"))
    db.session.commit()
    return workspace, project


def _message(project, role="assistant", content=MESSAGE_BODY):
    message = ProjectMessage(project_id=project.id, role=role, content=content)
    db.session.add(message)
    db.session.commit()
    return message


def _comments_url(project, message):
    return f"/workspaces/api/projects/{project.id}/messages/{message.id}/review-comments"


class TestReviewCommentCrud:
    def test_member_can_comment_on_assistant_message_with_block_anchor(
        self, client, make_user, login
    ):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        _, project = _setup(owner, [member])
        message = _message(project)
        login(email="member@example.com")

        response = client.post(
            _comments_url(project, message),
            json={"body": "This block is wrong.", "block_index": 0, "line_start": 1, "line_end": 2},
        )
        assert response.status_code == 201
        data = response.get_json()
        assert data["author_username"] == "member"
        assert data["block_index"] == 0
        assert data["line_start"] == 1
        assert data["line_end"] == 2
        assert data["resolved"] is False

    def test_whole_message_comment_has_no_anchor(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        data = client.post(
            _comments_url(project, message), json={"body": "Overall note."}
        ).get_json()
        assert data["block_index"] is None
        assert data["line_start"] is None
        assert data["line_end"] is None

    def test_empty_body_rejected(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        assert client.post(_comments_url(project, message), json={"body": "   "}).status_code == 400

    def test_overlong_body_rejected(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        response = client.post(
            _comments_url(project, message),
            json={"body": "x" * (REVIEW_COMMENT_MAX_LENGTH + 1)},
        )
        assert response.status_code == 400

    def test_cannot_comment_on_user_message(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project, role="user", content="Please fix this.")
        login(email="owner@example.com")
        assert (
            client.post(_comments_url(project, message), json={"body": "nope"}).status_code == 400
        )

    def test_block_index_out_of_range_rejected(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        response = client.post(
            _comments_url(project, message), json={"body": "x", "block_index": 5}
        )
        assert response.status_code == 400
        assert "block_index" in response.get_json()["error"]

    def test_invalid_line_range_rejected(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        response = client.post(
            _comments_url(project, message),
            json={"body": "x", "line_start": 5, "line_end": 2},
        )
        assert response.status_code == 400

    def test_list_threads_with_replies(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        root = client.post(_comments_url(project, message), json={"body": "Root"}).get_json()
        client.post(
            _comments_url(project, message),
            json={"body": "Reply", "parent_id": root["id"]},
        )
        payload = client.get(_comments_url(project, message)).get_json()
        assert len(payload["items"]) == 1
        assert payload["items"][0]["body"] == "Root"
        assert [r["body"] for r in payload["items"][0]["replies"]] == ["Reply"]

    def test_reply_must_reference_same_message(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        first = _message(project)
        second = _message(project)
        login(email="owner@example.com")
        root = client.post(_comments_url(project, first), json={"body": "Root"}).get_json()
        response = client.post(
            _comments_url(project, second),
            json={"body": "Reply", "parent_id": root["id"]},
        )
        assert response.status_code == 400

    def test_reply_depth_limited(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        root = client.post(_comments_url(project, message), json={"body": "Root"}).get_json()
        reply = client.post(
            _comments_url(project, message),
            json={"body": "Reply", "parent_id": root["id"]},
        ).get_json()
        response = client.post(
            _comments_url(project, message),
            json={"body": "Deep", "parent_id": reply["id"]},
        )
        assert response.status_code == 400


class TestResolution:
    def test_author_can_resolve_and_unresolve(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        comment = client.post(_comments_url(project, message), json={"body": "Fix me"}).get_json()
        url = f"{_comments_url(project, message)}/{comment['id']}"

        resolved = client.patch(url, json={"resolved": True}).get_json()
        assert resolved["resolved"] is True
        assert resolved["resolved_by_username"] == "owner"
        assert resolved["resolved_at"] is not None

        reopened = client.patch(url, json={"resolved": False}).get_json()
        assert reopened["resolved"] is False
        assert reopened["resolved_by"] is None
        assert reopened["resolved_at"] is None

    def test_owner_can_resolve_others_comment(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        _, project = _setup(owner, [member])
        message = _message(project)

        login(email="member@example.com")
        comment = client.post(
            _comments_url(project, message), json={"body": "From member"}
        ).get_json()

        login(email="owner@example.com")
        response = client.patch(
            f"{_comments_url(project, message)}/{comment['id']}", json={"resolved": True}
        )
        assert response.status_code == 200
        assert response.get_json()["resolved"] is True
        assert response.get_json()["resolved_by_username"] == "owner"

    def test_other_member_cannot_resolve(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        author = _create_user("author", "author@example.com")
        other = _create_user("other", "other@example.com")
        _, project = _setup(owner, [author, other])
        message = _message(project)

        login(email="author@example.com")
        comment = client.post(_comments_url(project, message), json={"body": "Mine"}).get_json()

        login(email="other@example.com")
        response = client.patch(
            f"{_comments_url(project, message)}/{comment['id']}", json={"resolved": True}
        )
        assert response.status_code == 403

    def test_cannot_resolve_reply(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        root = client.post(_comments_url(project, message), json={"body": "Root"}).get_json()
        reply = client.post(
            _comments_url(project, message),
            json={"body": "Reply", "parent_id": root["id"]},
        ).get_json()
        response = client.patch(
            f"{_comments_url(project, message)}/{reply['id']}", json={"resolved": True}
        )
        assert response.status_code == 400


class TestPermissions:
    def test_non_member_cannot_access(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        make_user(username="outsider", email="outsider@example.com")
        login(email="outsider@example.com")
        assert client.get(_comments_url(project, message)).status_code == 404
        assert (
            client.post(_comments_url(project, message), json={"body": "Intrusion"}).status_code
            == 404
        )
        assert (
            client.get(f"/workspaces/api/projects/{project.id}/review-summary").status_code == 404
        )

    def test_author_can_delete_own_comment(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        _, project = _setup(owner, [member])
        message = _message(project)
        login(email="member@example.com")
        comment = client.post(_comments_url(project, message), json={"body": "Bye"}).get_json()
        response = client.delete(f"{_comments_url(project, message)}/{comment['id']}")
        assert response.status_code == 200

    def test_owner_can_delete_others_comment(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        _, project = _setup(owner, [member])
        message = _message(project)
        login(email="member@example.com")
        comment = client.post(_comments_url(project, message), json={"body": "Bye"}).get_json()
        login(email="owner@example.com")
        assert (
            client.delete(f"{_comments_url(project, message)}/{comment['id']}").status_code == 200
        )

    def test_other_member_cannot_delete(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        author = _create_user("author", "author@example.com")
        other = _create_user("other", "other@example.com")
        _, project = _setup(owner, [author, other])
        message = _message(project)
        login(email="author@example.com")
        comment = client.post(_comments_url(project, message), json={"body": "Keep"}).get_json()
        login(email="other@example.com")
        assert (
            client.delete(f"{_comments_url(project, message)}/{comment['id']}").status_code == 403
        )


class TestMentions:
    def test_mention_notifies_active_member(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        _, project = _setup(owner, [member])
        message = _message(project)
        login(email="owner@example.com")
        client.post(
            _comments_url(project, message),
            json={"body": "@member please check this block."},
        )
        assert Notification.query.filter_by(user_id=member.id, type="mention").count() == 1

    def test_unknown_mention_creates_no_notification(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        client.post(_comments_url(project, message), json={"body": "@ghost hello"})
        assert Notification.query.filter_by(type="mention").all() == []


class TestReviewSummary:
    def test_counts_open_and_resolved_threads(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        first = client.post(_comments_url(project, message), json={"body": "A"}).get_json()
        client.post(_comments_url(project, message), json={"body": "B"})
        client.patch(f"{_comments_url(project, message)}/{first['id']}", json={"resolved": True})
        payload = client.get(f"/workspaces/api/projects/{project.id}/review-summary").get_json()
        assert payload["total"] == 2
        assert payload["resolved"] == 1
        assert payload["open"] == 1
        assert len(payload["threads"]) == 2

    def test_reply_included_in_reply_count(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        root = client.post(_comments_url(project, message), json={"body": "Root"}).get_json()
        client.post(
            _comments_url(project, message), json={"body": "Reply", "parent_id": root["id"]}
        )
        payload = client.get(f"/workspaces/api/projects/{project.id}/review-summary").get_json()
        assert payload["total"] == 1
        assert payload["threads"][0]["reply_count"] == 1


class TestModel:
    def test_reply_cascades_when_root_deleted(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        _, project = _setup(owner)
        message = _message(project)
        login(email="owner@example.com")
        root = client.post(_comments_url(project, message), json={"body": "Root"}).get_json()
        client.post(
            _comments_url(project, message), json={"body": "Reply", "parent_id": root["id"]}
        )
        assert ReviewComment.query.count() == 2
        client.delete(f"{_comments_url(project, message)}/{root['id']}")
        assert ReviewComment.query.count() == 0
