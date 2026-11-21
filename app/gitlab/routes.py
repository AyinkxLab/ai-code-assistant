"""GitLab OAuth connection and repository listing (#677)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from flask import abort, current_app, jsonify, redirect, request, session
from flask_login import current_user, login_required

from app.extensions import db
from app.gitlab import bp
from app.models import GitlabAccount, Workspace
from app.services import gitlab, integration_credentials


def _workspace(workspace_id: int) -> Workspace:
    workspace = Workspace.query.filter_by(id=workspace_id).first()
    if workspace is None or workspace.user_id != current_user.id:
        abort(404)
    return workspace


def _structured_error(exc: Exception, status: int = 400):
    return jsonify({"error": str(exc), "code": getattr(exc, "code", "error")}), status


@bp.route("/workspaces/<int:workspace_id>/connect")
@login_required
def connect(workspace_id: int):
    """Start the OAuth flow for a workspace the caller owns."""
    _workspace(workspace_id)
    state = secrets.token_urlsafe(24)
    session["gitlab_oauth_state"] = state
    session["gitlab_workspace_id"] = workspace_id
    try:
        return redirect(gitlab.authorize_url(state))
    except gitlab.GitlabConfigError as exc:
        return _structured_error(exc, 503)


@bp.route("/callback")
@login_required
def callback():
    """Exchange the OAuth code and store the encrypted access token."""
    state = (request.args.get("state") or "").strip()
    expected = session.pop("gitlab_oauth_state", None)
    workspace_id = session.pop("gitlab_workspace_id", None)
    if not expected or not state or not secrets.compare_digest(state, expected):
        return (
            jsonify({"error": "Invalid OAuth state.", "code": "invalid_oauth_state"}),
            400,
        )
    workspace = Workspace.query.filter_by(id=workspace_id).first()
    if workspace is None or workspace.user_id != current_user.id:
        abort(404)

    try:
        token = gitlab.exchange_code(request.args.get("code") or "")
        profile = gitlab.fetch_user(token)
    except gitlab.GitlabError as exc:
        return _structured_error(exc, 502 if isinstance(exc, gitlab.GitlabApiError) else 400)

    user_id = profile.get("id")
    username = profile.get("username")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not username:
        return (
            jsonify({"error": "GitLab returned an invalid profile.", "code": "gitlab_api_error"}),
            502,
        )

    try:
        integration_credentials.set_credential(
            workspace.id, "gitlab", "access_token", token, user=current_user
        )
    except integration_credentials.IntegrationCredentialError as exc:
        return _structured_error(exc, 400)

    account = GitlabAccount.query.filter_by(
        workspace_id=workspace.id, gitlab_user_id=user_id
    ).first()
    if account is None:
        account = GitlabAccount(
            workspace_id=workspace.id, gitlab_user_id=user_id, username=str(username)
        )
        db.session.add(account)
    account.username = str(username)
    account.display_name = profile.get("name")
    account.scopes = current_app.config.get("GITLAB_SCOPES")
    account.revoked_at = None
    db.session.commit()
    return jsonify(account.to_dict())


@bp.route("/api/workspaces/<int:workspace_id>/repositories")
@login_required
def repositories(workspace_id: int):
    """List repositories for the connected GitLab account (bounded)."""
    _workspace(workspace_id)
    try:
        credential = integration_credentials.get_credential(
            workspace_id, "gitlab", "access_token", user=current_user
        )
    except integration_credentials.IntegrationCredentialNotFound as exc:
        return _structured_error(exc, 404)
    except integration_credentials.IntegrationCredentialError as exc:
        return _structured_error(exc, 400)
    try:
        token = integration_credentials.decrypt_for_use(credential)
        items = gitlab.list_repositories(token)
    except gitlab.GitlabAuthError as exc:
        return _structured_error(exc, 401)
    except (gitlab.GitlabError, integration_credentials.IntegrationCredentialError) as exc:
        return _structured_error(exc, 502)
    return jsonify({"repositories": items})


@bp.route("/api/workspaces/<int:workspace_id>/revoke", methods=["POST"])
@login_required
def revoke(workspace_id: int):
    """Revoke the GitLab token, delete the credential, and record revocation."""
    _workspace(workspace_id)
    try:
        credential = integration_credentials.get_credential(
            workspace_id, "gitlab", "access_token", user=current_user
        )
    except integration_credentials.IntegrationCredentialError as exc:
        return _structured_error(exc, 404)
    token = integration_credentials.decrypt_for_use(credential)
    gitlab.revoke_token(token)
    integration_credentials.delete_credential(
        workspace_id, "gitlab", "access_token", user=current_user
    )
    GitlabAccount.query.filter_by(workspace_id=workspace_id).update(
        {"revoked_at": datetime.now(UTC)}
    )
    db.session.commit()
    return jsonify({"revoked": True})
