"""MFA enrollment, login challenge, and workspace enforcement (#618)."""

from app.models import Workspace, WorkspaceSettings
from app.services import mfa

PASSWORD = "supersecret123"


def test_totp_roundtrip_and_window():
    secret = mfa.generate_secret()
    code = mfa.totp(secret, at=1_700_000_000)

    assert mfa.verify_code(secret, code, at=1_700_000_000)
    assert mfa.verify_code(secret, code, at=1_700_000_030)
    assert not mfa.verify_code(secret, "abcdef")
    assert not mfa.verify_code(None, code)


def _workspace_requiring_mfa(db, owner):
    workspace = Workspace(user_id=owner.id, name="MFA Org")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(WorkspaceSettings(workspace_id=workspace.id, require_mfa=True))
    db.session.commit()
    return workspace


def test_unenrolled_login_is_blocked_when_workspace_requires_mfa(client, db, make_user):
    user = make_user()
    _workspace_requiring_mfa(db, user)

    response = client.post("/auth/login", data={"email": user.email, "password": PASSWORD})

    assert response.status_code == 200
    assert b"Multi-factor authentication is required" in response.data
    with client.session_transaction() as session:
        assert "_user_id" not in session


def test_break_glass_email_skips_enforcement(client, db, make_user, app):
    user = make_user(email="ops@example.com")
    _workspace_requiring_mfa(db, user)
    app.config["MFA_BREAK_GLASS_EMAILS"] = "ops@example.com"

    response = client.post(
        "/auth/login", data={"email": user.email, "password": PASSWORD}, follow_redirects=False
    )

    assert response.status_code == 302


def test_enrolled_user_completes_the_login_challenge(client, db, make_user):
    user = make_user()
    user.mfa_secret = mfa.generate_secret()
    user.mfa_enabled = True
    db.session.commit()

    response = client.post(
        "/auth/login", data={"email": user.email, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 302
    assert "/auth/mfa" in response.headers["Location"]

    response = client.post(
        "/auth/mfa", data={"code": mfa.totp(user.mfa_secret)}, follow_redirects=False
    )
    assert response.status_code == 302


def test_privileged_only_policy_ignores_viewers(client, db, make_user):
    from app.models import WorkspaceMember
    from app.models.workspace_member import ROLE_VIEWER

    owner = make_user("owner", "owner@example.com")
    viewer = make_user("viewer", "viewer@example.com")
    workspace = _workspace_requiring_mfa(db, owner)
    workspace.settings.mfa_privileged_only = True
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id, role=ROLE_VIEWER))
    db.session.commit()

    assert mfa.mfa_required_for(owner) is True
    assert mfa.mfa_required_for(viewer) is False
