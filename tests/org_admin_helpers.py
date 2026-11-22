"""Shared builders for the organization admin console tests (#626-#633)."""

from datetime import UTC, datetime

from app.extensions import db
from app.models import Project, UsageRecord, User, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_VIEWER, STATUS_ACTIVE


def _user(username, email, *, admin=False):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    user.is_admin = admin
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner, name="Acme platform"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _member(workspace, user, role=ROLE_VIEWER, status=STATUS_ACTIVE):
    membership = WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role=role, status=status
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def _usage(owner, workspace, *, tokens=30, cost=0.01):
    record = UsageRecord(
        user_id=owner.id,
        workspace_id=workspace.id,
        source="chat",
        provider="openai",
        model="gpt-4o",
        prompt_tokens=tokens - 5,
        completion_tokens=5,
        total_tokens=tokens,
        tokens_estimated=True,
        cost_estimate=cost,
        status="success",
        created_at=datetime.now(UTC),
    )
    db.session.add(record)
    db.session.commit()
    return record


def _project(owner, workspace):
    project = Project(
        workspace_id=workspace.id,
        user_id=owner.id,
        name="Payments",
        source="archive",
        status="ready",
    )
    db.session.add(project)
    db.session.commit()
    return project
