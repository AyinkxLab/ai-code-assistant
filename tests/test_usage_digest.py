from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models.usage_record import UsageRecord
from app.models.user import User
from app.models.workspace import Workspace
from app.services.usage_digest import (
    DIGEST_WINDOW,
    MAX_SOURCE_ROWS,
    build_digest,
)

END = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
START = END - DIGEST_WINDOW


@pytest.fixture
def owner(app):
    user = User(username="digest-owner", email="digest-owner@example.test")
    user.set_password("not-used-in-these-tests")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def workspace(app, owner):
    ws = Workspace(user_id=owner.id, name="Digest Workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _record(
    *,
    user=None,
    workspace=None,
    at=END - timedelta(days=1),
    tokens=100,
    cost=0.5,
    status="success",
    source="chat",
):
    row = UsageRecord(
        user_id=user.id if user else None,
        workspace_id=workspace.id if workspace else None,
        source=source,
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=tokens,
        completion_tokens=0,
        total_tokens=tokens,
        cost_estimate=cost,
        status=status,
        created_at=at,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_totals_include_only_the_half_open_window(owner):
    _record(user=owner, at=START, tokens=10, cost=0.1)  # start: included
    _record(user=owner, at=END - timedelta(seconds=1), tokens=20, cost=0.2)  # inside
    _record(user=owner, at=END, tokens=40, cost=0.4)  # end: next digest
    _record(user=owner, at=START - timedelta(seconds=1), tokens=80, cost=0.8)  # before

    summary = build_digest("user", owner.id, end=END)

    assert summary.totals.calls == 2
    assert summary.totals.total_tokens == 30
    assert summary.totals.cost_estimate == pytest.approx(0.3)


def test_denied_rows_are_excluded_but_null_status_rows_are_kept(owner):
    _record(user=owner, tokens=100, status="success")
    _record(user=owner, tokens=500, status="denied")
    _record(user=owner, tokens=50, status=None)

    summary = build_digest("user", owner.id, end=END)

    assert summary.totals.calls == 2
    assert summary.totals.total_tokens == 150


def test_unpriced_rows_are_counted_separately_not_as_zero(owner):
    _record(user=owner, tokens=100, cost=0.5)
    _record(user=owner, tokens=300, cost=None)

    summary = build_digest("user", owner.id, end=END)

    assert summary.totals.calls == 2
    assert summary.totals.total_tokens == 400
    assert summary.totals.cost_estimate == pytest.approx(0.5)
    assert summary.totals.unpriced_calls == 1


def test_user_scope_ignores_other_users(owner):
    other = User(username="digest-other", email="digest-other@example.test")
    other.set_password("not-used")
    db.session.add(other)
    db.session.commit()

    _record(user=owner, tokens=100)
    _record(user=other, tokens=900)

    assert build_digest("user", owner.id, end=END).totals.total_tokens == 100


def test_workspace_scope_counts_rows_with_no_user(owner, workspace):
    # Background and CLI rows can have no user_id, so the workspace digest
    # must match on workspace_id alone.
    _record(workspace=workspace, user=None, tokens=250)
    _record(user=owner, tokens=999)  # not in this workspace

    summary = build_digest("workspace", workspace.id, end=END)

    assert summary.totals.calls == 1
    assert summary.totals.total_tokens == 250


def test_source_breakdown_is_bounded_and_flags_truncation(owner):
    for i in range(MAX_SOURCE_ROWS + 2):
        _record(user=owner, source=f"source-{i}", tokens=i + 1)

    summary = build_digest("user", owner.id, end=END)

    assert len(summary.by_source) == MAX_SOURCE_ROWS
    assert summary.sources_truncated is True
    # Largest token count first.
    assert summary.by_source[0][0] == f"source-{MAX_SOURCE_ROWS + 1}"


def test_empty_scope_returns_zero_totals_not_an_error(owner):
    summary = build_digest("user", owner.id, end=END)

    assert summary.totals.calls == 0
    assert summary.totals.total_tokens == 0
    assert summary.totals.cost_estimate == 0.0
    assert summary.by_source == []
    assert summary.sources_truncated is False


def test_unknown_scope_is_rejected(owner):
    with pytest.raises(ValueError):
        build_digest("org", owner.id, end=END)
