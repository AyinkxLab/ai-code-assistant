"""Tests for the member activity feed and the owner-only audit log (#145/#151).

Covers role scoping (members never see the audit-sensitive subset), cursor
pagination, filters, and the uniform no-existence-oracle behavior.
"""

import io
import zipfile
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import Project, User, Workspace, WorkspaceMember
from app.models.activity_event import (
    AUDIT_EVENT_TYPES,
    EVENT_AI_ANALYSIS_RUN,
    EVENT_COMMENT_ADDED,
    EVENT_CONVERSATION_CREATED,
    EVENT_FILE_UPLOADED,
    EVENT_MEMBER_ADDED,
    EVENT_MEMBER_REMOVED,
    ActivityEvent,
)


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _setup(owner, member):
    workspace = Workspace(user_id=owner.id, name="Activity workspace")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="viewer"))
    db.session.commit()
    return workspace


def _add_event(workspace_id, actor_id, event_type, metadata=None, minutes_ago=0):
    event = ActivityEvent(
        workspace_id=workspace_id,
        actor_id=actor_id,
        event_type=event_type,
        event_metadata=metadata,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    db.session.add(event)
    db.session.commit()
    return event


class TestActivityFeed:
    def test_member_never_sees_audit_subset(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        _add_event(workspace.id, owner.id, EVENT_MEMBER_ADDED)
        _add_event(workspace.id, owner.id, EVENT_COMMENT_ADDED)
        login(email="member@example.com")
        data = client.get(f"/workspaces/api/workspaces/{workspace.id}/activity").get_json()
        assert len(data["items"]) == 1
        assert data["items"][0]["event_type"] == EVENT_COMMENT_ADDED

    def test_owner_sees_all(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        _add_event(workspace.id, owner.id, EVENT_MEMBER_ADDED)
        _add_event(workspace.id, owner.id, EVENT_COMMENT_ADDED)
        login(email="owner@example.com")
        data = client.get(f"/workspaces/api/workspaces/{workspace.id}/activity").get_json()
        assert len(data["items"]) == 2

    def test_member_feed_never_includes_metadata(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        _add_event(workspace.id, owner.id, EVENT_AI_ANALYSIS_RUN, metadata={"kind": "bugs"})
        login(email="member@example.com")
        data = client.get(f"/workspaces/api/workspaces/{workspace.id}/activity").get_json()
        assert len(data["items"]) == 1
        assert "metadata" not in data["items"][0]

    def test_event_type_filter(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        _add_event(workspace.id, owner.id, EVENT_COMMENT_ADDED)
        _add_event(workspace.id, owner.id, EVENT_AI_ANALYSIS_RUN)
        login(email="member@example.com")
        data = client.get(
            f"/workspaces/api/workspaces/{workspace.id}/activity",
            query_string={"event_type": EVENT_COMMENT_ADDED},
        ).get_json()
        assert len(data["items"]) == 1
        assert data["items"][0]["event_type"] == EVENT_COMMENT_ADDED

    def test_cursor_pagination(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        for i in range(3):
            _add_event(workspace.id, owner.id, EVENT_COMMENT_ADDED, minutes_ago=i)
        login(email="member@example.com")
        first = client.get(
            f"/workspaces/api/workspaces/{workspace.id}/activity", query_string={"per_page": 2}
        ).get_json()
        assert len(first["items"]) == 2
        assert first["next_cursor"] is not None
        second = client.get(
            f"/workspaces/api/workspaces/{workspace.id}/activity",
            query_string={"per_page": 2, "before": first["next_cursor"]},
        ).get_json()
        assert len(second["items"]) == 1
        assert second["next_cursor"] is None

    def test_actor_filter(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        _add_event(workspace.id, owner.id, EVENT_COMMENT_ADDED)
        _add_event(workspace.id, member.id, EVENT_COMMENT_ADDED)
        login(email="member@example.com")
        data = client.get(
            f"/workspaces/api/workspaces/{workspace.id}/activity",
            query_string={"actor": "owner"},
        ).get_json()
        assert len(data["items"]) == 1

    def test_non_member_gets_404(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        outsider = _create_user("outsider", "outsider@example.com")
        workspace = _setup(owner, outsider)
        make_user(username="stranger", email="stranger@example.com")
        login(email="stranger@example.com")
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/activity").status_code == 404


class TestAuditLog:
    def test_audit_is_owner_only(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        _add_event(workspace.id, owner.id, EVENT_MEMBER_ADDED)
        login(email="member@example.com")
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/audit").status_code == 403
        login(email="owner@example.com")
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/audit").status_code == 200

    def test_audit_only_contains_audit_subset(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _setup(owner, _create_user("member", "member@example.com"))
        _add_event(workspace.id, owner.id, EVENT_MEMBER_ADDED)
        _add_event(workspace.id, owner.id, EVENT_COMMENT_ADDED)
        _add_event(workspace.id, owner.id, EVENT_MEMBER_REMOVED)
        login(email="owner@example.com")
        data = client.get(f"/workspaces/api/workspaces/{workspace.id}/audit").get_json()
        types = {item["event_type"] for item in data["items"]}
        assert types == {EVENT_MEMBER_ADDED, EVENT_MEMBER_REMOVED}
        assert types.issubset(set(AUDIT_EVENT_TYPES))

    def test_audit_includes_metadata_for_owner(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _setup(owner, _create_user("member", "member@example.com"))
        _add_event(workspace.id, owner.id, EVENT_MEMBER_ADDED, metadata={"role": "viewer"})
        login(email="owner@example.com")
        data = client.get(f"/workspaces/api/workspaces/{workspace.id}/audit").get_json()
        assert data["items"][0]["metadata"]["role"] == "viewer"

    def test_audit_pagination_and_filter(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _setup(owner, _create_user("member", "member@example.com"))
        for i in range(5):
            _add_event(workspace.id, owner.id, EVENT_MEMBER_ADDED, minutes_ago=i)
        login(email="owner@example.com")
        data = client.get(
            f"/workspaces/api/workspaces/{workspace.id}/audit",
            query_string={"per_page": 2, "page": 2},
        ).get_json()
        assert data["total"] == 5
        assert len(data["items"]) == 2


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _project(workspace, owner, name="Activity project"):
    project = Project(
        workspace_id=workspace.id,
        user_id=owner.id,
        name=name,
        source="archive",
        status="ready",
    )
    db.session.add(project)
    db.session.commit()
    return project


class TestActivityEventRecording:
    """The four workspace actions from #55 must append feed events."""

    def test_conversation_created_is_recorded(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        login(email="owner@example.com")

        response = client.post(
            "/chat/conversations",
            json={"title": "Filed conversation", "workspace_id": workspace.id},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 201

        event = ActivityEvent.query.filter_by(
            workspace_id=workspace.id, event_type=EVENT_CONVERSATION_CREATED
        ).one()
        assert event.actor_id == owner.id
        assert event.target_type == "conversations"
        assert event.target_id == response.get_json()["id"]

    def test_uploaded_archive_is_recorded(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        login(email="owner@example.com")

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/projects",
            data={"file": (io.BytesIO(_zip_bytes([("main.py", "print('hi')")])), "project.zip")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 201

        event = ActivityEvent.query.filter_by(
            workspace_id=workspace.id, event_type=EVENT_FILE_UPLOADED
        ).one()
        assert event.actor_id == owner.id
        assert event.target_id == response.get_json()["id"]

    def test_analysis_and_comment_are_recorded(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        project = _project(workspace, owner)
        login(email="owner@example.com")

        assert (
            client.post(
                f"/workspaces/api/projects/{project.id}/analyze",
                json={"kind": "architecture"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/workspaces/api/projects/{project.id}/comments",
                json={"content": "Looks good"},
            ).status_code
            == 201
        )

        recorded = {
            event.event_type
            for event in ActivityEvent.query.filter_by(workspace_id=workspace.id).all()
        }
        assert EVENT_AI_ANALYSIS_RUN in recorded
        assert EVENT_COMMENT_ADDED in recorded

    def test_member_sees_recorded_events_and_page(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        login(email="owner@example.com")
        client.post(
            "/chat/conversations",
            json={"title": "Member-visible", "workspace_id": workspace.id},
            headers={"X-CSRFToken": "ignored"},
        )

        login(email="member@example.com")
        data = client.get(f"/workspaces/api/workspaces/{workspace.id}/activity").get_json()
        assert [item["event_type"] for item in data["items"]] == [EVENT_CONVERSATION_CREATED]

        page = client.get(f"/workspaces/{workspace.id}")
        assert page.status_code == 200
        assert b"Activity" in page.data
        assert b"Import a project" not in page.data


class TestActivityPagePagination:
    def test_page_pagination_is_scoped_to_workspace(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = _setup(owner, member)
        other_owner = _create_user("otherowner", "otherowner@example.com")
        other = _setup(other_owner, _create_user("other", "other@example.com"))
        for i in range(5):
            _add_event(workspace.id, owner.id, EVENT_COMMENT_ADDED, minutes_ago=i)
        _add_event(other.id, owner.id, EVENT_COMMENT_ADDED)

        login(email="member@example.com")
        data = client.get(
            f"/workspaces/api/workspaces/{workspace.id}/activity",
            query_string={"page": 2, "per_page": 2},
        ).get_json()
        assert data["total"] == 5
        assert data["page"] == 2
        assert data["per_page"] == 2
        assert len(data["items"]) == 2
        assert all(item["workspace_id"] == workspace.id for item in data["items"])
