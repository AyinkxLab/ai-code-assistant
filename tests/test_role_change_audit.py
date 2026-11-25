"""Role-change audit coverage (#604).

Every membership role assignment, change, and removal writes one
``role.changed`` event with before/after state, actor, target, and request id.
The event is visible through the owner audit view and the structured audit
logger. Failed or unauthorized mutations write nothing.
"""

import json
import logging

import pytest
from org_admin_helpers import _member, _user, _workspace

from app.extensions import db
from app.models import User, Workspace, WorkspaceMember
from app.models.activity_event import EVENT_ROLE_CHANGED
from app.services import chat_audit
from app.services.data_deletion import confirm_request, request_deletion


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def role_events(self) -> list[dict]:
        formatter = chat_audit.JsonFormatter()
        return [
            json.loads(formatter.format(record))
            for record in self.records
            if getattr(record, "audit_fields", {}).get("event") == "role.changed"
        ]


@pytest.fixture()
def capture():
    handler = _Capture()
    chat_audit.LOGGER.addHandler(handler)
    chat_audit.LOGGER.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        chat_audit.LOGGER.removeHandler(handler)


def _user_with_password(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace_for(owner):
    workspace = Workspace(user_id=owner.id, name="Audit workspace")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _audit_events(client, workspace_id):
    response = client.get(
        f"/workspaces/api/workspaces/{workspace_id}/audit",
        query_string={"event_type": EVENT_ROLE_CHANGED, "per_page": 50},
    )
    assert response.status_code == 200
    return response.get_json()["items"]


def _event_for(events, action):
    matches = [event for event in events if event["metadata"].get("action") == action]
    assert len(matches) == 1, [event["metadata"] for event in events]
    return matches


class TestRoleChangeAudit:
    def test_assignment_records_before_after_actor_target_and_request_id(
        self, client, login, capture
    ):
        owner = _user_with_password("owner", "owner@example.com")
        member = _user_with_password("member", "member@example.com")
        workspace = _workspace_for(owner)
        login(email="owner@example.com")

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "member", "role": "viewer"},
            headers={"X-Request-ID": "trace-assign"},
        )
        assert response.status_code == 201

        events = _audit_events(client, workspace.id)
        assert len(events) == 1
        event = events[0]
        assert event["event_type"] == EVENT_ROLE_CHANGED
        assert event["actor_username"] == "owner"
        assert event["target_type"] == "users"
        assert event["target_id"] == member.id
        metadata = event["metadata"]
        assert metadata["action"] == "assigned"
        assert metadata["old_role"] is None
        assert metadata["new_role"] == "viewer"
        assert metadata["actor_username"] == "owner"
        assert metadata["target_user_id"] == member.id
        assert metadata["target_username"] == "member"
        assert metadata["request_id"] == "trace-assign"
        assert metadata["membership_id"] is not None

        logged = capture.role_events()
        assert len(logged) == 1
        assert logged[0]["request_id"] == "trace-assign"
        assert logged[0]["user_id"] == owner.id
        assert logged[0]["target_user_id"] == member.id
        assert logged[0]["action"] == "assigned"
        assert logged[0]["old_role"] is None
        assert logged[0]["new_role"] == "viewer"

    def test_change_and_removal_record_both_states(self, client, login):
        owner = _user_with_password("owner", "owner@example.com")
        member = _user_with_password("member", "member@example.com")
        workspace = _workspace_for(owner)
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="viewer"))
        db.session.commit()
        login(email="owner@example.com")
        url = f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}"

        changed = client.patch(
            url, json={"role": "contributor"}, headers={"X-Request-ID": "trace-change"}
        )
        assert changed.status_code == 200
        removed = client.delete(url, headers={"X-Request-ID": "trace-remove"})
        assert removed.status_code == 200

        events = _audit_events(client, workspace.id)
        assert len(events) == 2
        change = _event_for(events, "changed")[0]
        assert change["metadata"]["old_role"] == "viewer"
        assert change["metadata"]["new_role"] == "contributor"
        assert change["metadata"]["request_id"] == "trace-change"
        removal = _event_for(events, "removed")[0]
        assert removal["metadata"]["old_role"] == "contributor"
        assert removal["metadata"]["new_role"] is None
        assert removal["metadata"]["request_id"] == "trace-remove"
        assert removal["actor_username"] == "owner"
        assert removal["target_id"] == member.id

    def test_failed_and_unauthorized_mutations_write_no_events(self, client, login):
        owner = _user_with_password("owner", "owner@example.com")
        member = _user_with_password("member", "member@example.com")
        _user_with_password("outsider", "outsider@example.com")
        workspace = _workspace_for(owner)
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="viewer"))
        db.session.commit()
        login(email="owner@example.com")
        url = f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}"

        assert client.patch(url, json={"role": "admin"}).status_code == 400
        assert (
            client.post(
                f"/workspaces/api/workspaces/{workspace.id}/members",
                json={"username": "ghost", "role": "contributor"},
            ).status_code
            == 404
        )
        login(email="outsider@example.com")
        assert client.patch(url, json={"role": "contributor"}).status_code == 404

        login(email="owner@example.com")
        assert _audit_events(client, workspace.id) == []

    def test_self_leave_records_role_removal(self, client, login):
        owner = _user_with_password("owner", "owner@example.com")
        member = _user_with_password("member", "member@example.com")
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="contributor")
        )
        db.session.commit()
        login(email="member@example.com")

        response = client.delete(
            f"/workspaces/api/workspaces/{workspace.id}/membership",
            headers={"X-Request-ID": "trace-leave"},
        )
        assert response.status_code == 200

        login(email="owner@example.com")
        removal = _event_for(_audit_events(client, workspace.id), "removed")[0]
        assert removal["actor_username"] == "member"
        assert removal["target_id"] == member.id
        assert removal["metadata"]["old_role"] == "contributor"
        assert removal["metadata"]["new_role"] is None
        assert removal["metadata"]["request_id"] == "trace-leave"

    def test_organization_admin_role_change_is_audited(self, client, login):
        owner = _user("owner", "owner@example.com")
        member = _user("bob", "bob@example.com")
        workspace = _workspace(owner)
        _member(workspace, member, role="viewer")
        login(email="owner@example.com")

        response = client.post(
            f"/admin/organizations/{workspace.id}/members",
            data={"action": "role", "user_id": member.id, "role": "admin", "confirm": "yes"},
            headers={"X-Request-ID": "trace-admin"},
            follow_redirects=True,
        )
        assert response.status_code == 200

        change = _event_for(_audit_events(client, workspace.id), "changed")[0]
        assert change["metadata"]["old_role"] == "viewer"
        assert change["metadata"]["new_role"] == "admin"
        assert change["metadata"]["actor_username"] == "owner"
        assert change["metadata"]["target_username"] == "bob"
        assert change["metadata"]["request_id"] == "trace-admin"

    def test_ownership_transfer_records_both_role_transitions(self, client, login):
        owner = _user_with_password("owner", "owner@example.com")
        target = _user_with_password("target", "target@example.com")
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=target.id, role="contributor")
        )
        db.session.commit()
        login(email="owner@example.com")

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": target.id},
            headers={"X-Request-ID": "trace-transfer"},
        )
        assert response.status_code == 200

        login(email="target@example.com")
        events = _audit_events(client, workspace.id)
        assert len(events) == 2
        assert {event["metadata"]["request_id"] for event in events} == {"trace-transfer"}
        assert {event["metadata"]["target_user_id"] for event in events} == {
            owner.id,
            target.id,
        }
        transitions = {
            event["metadata"]["target_user_id"]: (
                event["metadata"]["old_role"],
                event["metadata"]["new_role"],
            )
            for event in events
        }
        assert transitions[owner.id] == ("owner", "contributor")
        assert transitions[target.id] == ("contributor", "owner")

    def test_account_deletion_records_membership_role_removal(self, client, login):
        user = _user_with_password("leaving", "leaving@example.com")
        owner = _user_with_password("owner", "owner@example.com")
        workspace = _workspace_for(owner)
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="viewer"))
        db.session.commit()
        login(email="leaving@example.com")

        row = request_deletion(user, mode="anonymization")
        confirm_request(row.id, user, "delete my data", commit=False)
        db.session.commit()

        login(email="owner@example.com")
        removal = _event_for(_audit_events(client, workspace.id), "removed")[0]
        assert removal["metadata"]["old_role"] == "viewer"
        assert removal["metadata"]["new_role"] is None
        assert removal["metadata"]["target_user_id"] == user.id
        assert removal["actor_username"] == f"deleted-user-{user.id}"
