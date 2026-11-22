"""Enterprise adoption telemetry tests (#645).

Counters must use fixed enum labels only. User ids, workspace ids, emails,
domains, tokens, and other identifiers/secrets are never label values.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import User, Workspace, WorkspaceDomain
from app.observability.enterprise import (
    FEATURE_AUDIT_EXPORT,
    FEATURE_CUSTOM_ROLES,
    FEATURE_SCIM,
    FEATURE_SSO,
    record_audit_export,
    record_custom_role_event,
    record_enterprise_event,
    record_scim_event,
    record_sso_event,
)
from app.observability.metrics import REGISTRY


@pytest.fixture(autouse=True)
def _reset_metrics():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def _body() -> str:
    return REGISTRY.render()


def _admin(db):
    user = User(username="admin", email="admin@example.com")
    user.set_password("supersecret123")
    user.is_admin = True
    db.session.add(user)
    db.session.commit()
    return user


def test_feature_counters_use_fixed_labels():
    record_sso_event("enforce", "success")
    record_scim_event("provision", "success")
    record_custom_role_event("create", "denied")
    record_audit_export("csv", "success")

    body = _body()
    assert 'enterprise_sso_events_total{action="enforce",outcome="success"} 1' in body
    assert 'enterprise_scim_events_total{action="provision",outcome="success"} 1' in body
    assert 'enterprise_custom_role_events_total{action="create",outcome="denied"} 1' in body
    assert 'enterprise_audit_export_events_total{format="csv",outcome="success"} 1' in body
    for feature in (FEATURE_SSO, FEATURE_SCIM, FEATURE_CUSTOM_ROLES, FEATURE_AUDIT_EXPORT):
        assert f'feature="{feature}"' in body


def test_unknown_values_collapse_to_other_and_never_leak_identifiers():
    secret = "sk-live-super-secret-token"
    email = "owner@example.com"
    workspace = "workspace-42"
    record_enterprise_event(secret, email, workspace)
    record_enterprise_event(
        "not-a-feature",
        "not-an-action",
        "not-an-outcome",
    )

    body = _body()
    assert secret not in body
    assert email not in body
    assert workspace not in body
    assert (
        'enterprise_feature_events_total{feature="other",action="other",outcome="other"} 2'
    ) in body


def test_domain_routes_emit_sso_events(client, make_user, login, db):
    user = make_user()
    login()
    workspace = Workspace(user_id=user.id, name="Acme")
    db.session.add(workspace)
    db.session.commit()

    response = client.post(
        "/auth/api/domains/challenge",
        json={"workspace_id": workspace.id, "domain": "example.com"},
    )

    assert response.status_code == 201
    body = _body()
    assert 'enterprise_sso_events_total{action="challenge",outcome="success"} 1' in body
    assert "example.com" not in body


def test_domain_permission_failure_is_counted_without_identifiers(client, make_user, login, db):
    owner = make_user(username="owner", email="owner@example.com")
    make_user(username="outsider", email="outsider@example.net")
    workspace = Workspace(user_id=owner.id, name="Acme")
    db.session.add(workspace)
    db.session.commit()
    login(email="outsider@example.net")

    response = client.post(
        "/auth/api/domains/challenge",
        json={"workspace_id": workspace.id, "domain": "example.com"},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "forbidden"
    body = _body()
    assert 'enterprise_sso_events_total{action="challenge",outcome="denied"} 1' in body
    assert "outsider@example.net" not in body
    assert "example.com" not in body


def test_audit_export_records_format_and_outcome(client, db, login):
    _admin(db)
    login(email="admin@example.com")

    response = client.get("/admin/audit/export?format=csv")

    assert response.status_code == 200
    body = _body()
    assert 'enterprise_audit_export_events_total{format="csv",outcome="success"} 1' in body
    assert 'feature="audit_export"' in body


def test_audit_export_invalid_format_is_structured_and_counted(client, db, login):
    _admin(db)
    login(email="admin@example.com")

    response = client.get("/admin/audit/export?format=xml")

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_input"
    body = _body()
    assert 'enterprise_audit_export_events_total{format="other",outcome="invalid"} 1' in body


def test_domain_row_does_not_become_a_label(client, make_user, login, db):
    user = make_user()
    login()
    workspace = Workspace(user_id=user.id, name="Acme")
    db.session.add(workspace)
    db.session.commit()
    domain = WorkspaceDomain(
        workspace_id=workspace.id,
        domain="customer.example",
        verification_token="top-secret-token",
        challenge_expires_at=datetime.now(UTC),
    )
    db.session.add(domain)
    db.session.commit()

    client.get(f"/auth/api/domains?workspace_id={workspace.id}")

    body = _body()
    assert "customer.example" not in body
    assert "top-secret-token" not in body
    # The list route is not an adoption mutation.
    assert "enterprise_sso_events_total{" not in body
