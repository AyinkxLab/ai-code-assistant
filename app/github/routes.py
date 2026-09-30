"""GitHub routes: OAuth connection, repository browser, and AI analysis.

Layout
------
Pages (HTML)
    /github/                                dashboard (connection status)
    /github/connect                         start OAuth flow
    /github/callback                        OAuth callback
    /github/repos                           repository browser
    /github/repos/<owner>/<repo>            repository detail
    /github/repos/<owner>/<repo>/issues     issue list
    /github/repos/<owner>/<repo>/issues/<n> issue detail
    /github/repos/<owner>/<repo>/pulls      pull request list
    /github/repos/<owner>/<repo>/pulls/<n>  pull request detail

API (JSON)
    /github/api/status                      connection status
    /github/api/repos                       list repositories
    /github/api/repos/.../tree              tree of a ref
    /github/api/repos/.../contents          file or directory contents
    /github/api/repos/.../commits           commit history
    /github/api/repos/.../issues             issue list
    /github/api/repos/.../issues/<n>        single issue + AI analysis
    /github/api/repos/.../pulls             pull request list
    /github/api/repos/.../pulls/<n>         single PR + AI analysis
    /github/api/repos/.../analyze-file      AI analysis of one file
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.github import bp
from app.models import GithubAccount
from app.services import analysis, ratelimit
from app.services.github import (
    GITHUB_AUTHORIZE_URL,
    GITHUB_TOKEN_URL,
    PAGE_SIZE_DEFAULT,
    PAGE_SIZE_MAX,
    GitHubClient,
    GitHubError,
    GitHubPage,
    get_github_client,
    github_error_payload,
    issue_payload,
    pull_request_payload,
    repo_payload,
    revoke_github_token,
    validate_full_name,
    validate_path,
)

#: Cap on the number of files whose last commit is resolved for the browser.
#: The lookups are batched into one GraphQL request, but keeping the count
#: bounded avoids pathological query sizes on very large repositories.
MAX_TREE_LAST_COMMITS = 100


# ---------------------------------------------------------------------------
# OAuth connection
# ---------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    """Dashboard showing the user's GitHub connection status."""
    account = GithubAccount.query.filter_by(user_id=current_user.id).first()
    cancelled = request.args.get("cancelled") == "1"
    return render_template("github/index.html", account=account, cancelled=cancelled)


@bp.route("/connect")
@login_required
def connect():
    """Start the OAuth flow by redirecting the user to GitHub."""
    scopes = current_app.config.get("GITHUB_SCOPES") or "read:user repo"
    params = {
        "client_id": current_app.config["GITHUB_CLIENT_ID"],
        "redirect_uri": current_app.config.get("GITHUB_REDIRECT_URI")
        or url_for("github.callback", _external=True),
        "scope": scopes,
        "state": _new_state(),
        "allow_signup": "false",
    }
    return redirect(f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}")


@bp.route("/callback")
@login_required
def callback():
    """Exchange the authorization code for a token and store the connection."""
    key = _callback_limit_key()
    blocked, retry_after = _callback_blocked()
    if blocked:
        return _throttled_response(retry_after)

    error = request.args.get("error")
    if error:
        ratelimit.record(key)
        if error == "access_denied":
            flash(
                "GitHub connection cancelled. No access was granted. "
                "Use the Connect GitHub button below whenever you're ready to retry.",
                "info",
            )
            return redirect(url_for("github.index", cancelled=1))
        flash(
            "GitHub authorization failed. Please use the Connect GitHub button "
            "below to try again.",
            "error",
        )
        return redirect(url_for("github.index"))

    state = request.args.get("state")
    if state != _get_state():
        ratelimit.record(key)
        flash("GitHub authorization failed: state mismatch.", "error")
        return redirect(url_for("github.index"))

    code = request.args.get("code")
    if not code:
        ratelimit.record(key)
        flash("GitHub authorization failed: missing code.", "error")
        return redirect(url_for("github.index"))

    response = requests.post(
        GITHUB_TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": current_app.config["GITHUB_CLIENT_ID"],
            "client_secret": current_app.config["GITHUB_CLIENT_SECRET"],
            "code": code,
            "redirect_uri": current_app.config.get("GITHUB_REDIRECT_URI")
            or url_for("github.callback", _external=True),
        },
        timeout=current_app.config.get("GITHUB_REQUEST_TIMEOUT", 30),
    )
    try:
        token_data = response.json()
    except ValueError:
        token_data = {}
    if response.status_code >= 400 or "access_token" not in token_data:
        ratelimit.record(key)
        flash(
            "GitHub authorization failed. Please try connecting again.",
            "error",
        )
        return redirect(url_for("github.index"))

    token = token_data["access_token"]
    client = GitHubClient(token)
    try:
        user = client.get_user()
    except GitHubError as exc:
        ratelimit.record(key)
        flash(
            "Could not verify your GitHub account. Please try connecting again.",
            "error",
        )
        return redirect(url_for("github.index"))

    account = GithubAccount.query.filter_by(user_id=current_user.id).first()
    if account is None:
        account = GithubAccount(user_id=current_user.id)
        db.session.add(account)
    account.github_user_id = user["id"]
    account.github_username = user.get("login", "")
    account.scopes = token_data.get("scope", "")
    account.token_type = token_data.get("token_type", "bearer")
    account.set_access_token(token)
    account.set_refresh_token(token_data.get("refresh_token"))
    account.token_expires_at = (
        datetime.now(UTC)
        + timedelta(seconds=int(token_data["expires_in"]))
        if token_data.get("expires_in")
        else None
    )
    db.session.commit()

    from app.services.events import emit_event

    emit_event(
        "github.connected",
        data={"github_username": account.github_username},
        user_id=current_user.id,
    )

    # A successful connection proves the user's own state, so clear any
    # accumulated failure hits: legitimate connects are never throttled.
    ratelimit.clear(key)

    flash(
        f"Connected to GitHub as @{account.github_username}. "
        "You can now browse repositories and run AI analysis.",
        "success",
    )
    return redirect(url_for("github.index"))


@bp.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    """Remove the GitHub connection and its stored token."""
    account = GithubAccount.query.filter_by(user_id=current_user.id).first()
    if account is not None:
        try:
            from app.services.crypto import decrypt_secret

            revoke_github_token(decrypt_secret(account.access_token_encrypted))
        except ValueError:
            pass
        db.session.delete(account)
        db.session.commit()
        flash("Disconnected your GitHub account.", "info")
    return redirect(url_for("github.index"))


@bp.route("/api/status")
@login_required
def status():
    """Return GitHub connection status plus the remaining API quota.

    When the user is connected, a best-effort ``rate_limit`` summary is
    included so the dashboard can warn before the core API budget runs out.
    Only non-sensitive numeric fields are exposed — never the token or any
    response headers (issue #77).
    """
    account = GithubAccount.query.filter_by(user_id=current_user.id).first()
    payload: dict = {
        "connected": account is not None,
        "account": account.to_dict() if account else None,
    }
    if account is not None:
        payload["rate_limit"] = _rate_limit_summary()
    return jsonify(payload)


def _rate_limit_summary() -> dict:
    """Best-effort GitHub core rate-limit summary for the dashboard.

    Never fails the status request: if the budget cannot be read the payload
    reports ``available: false``. ``low`` is true when the remaining quota is
    at or below ``GITHUB_LOW_QUOTA_THRESHOLD``.
    """
    try:
        budget = _client().get_rate_limit()
    except GitHubError:
        budget = None
    if not budget:
        return {"available": False}
    threshold = current_app.config.get("GITHUB_LOW_QUOTA_THRESHOLD", 100)
    remaining = budget.get("remaining")
    return {
        "available": True,
        "limit": budget.get("limit"),
        "remaining": remaining,
        "reset": budget.get("reset"),
        "used": budget.get("used"),
        "threshold": threshold,
        "low": remaining is not None and remaining <= threshold,
    }


# ---------------------------------------------------------------------------
# OAuth state handling
# ---------------------------------------------------------------------------

_STATE_SESSION_KEY = "github_oauth_state"


def _new_state() -> str:
    import secrets

    state = secrets.token_urlsafe(32)
    from flask import session

    session[_STATE_SESSION_KEY] = state
    return state


def _get_state() -> str | None:
    from flask import session

    return session.pop(_STATE_SESSION_KEY, None)


def _callback_limit_key() -> str:
    """Build the OAuth callback limiter key for the current user and client IP."""
    return ratelimit.client_key(f"github_oauth:{current_user.get_id()}")


def _callback_blocked() -> tuple[bool, int]:
    """Return ``(blocked, retry_after)`` for repeated callback failures.

    Only failed callback attempts are recorded (see ``callback``), so a user who
    connects successfully is never affected; the limit exists purely to blunt
    brute-force ``state`` probing.
    """
    max_hits = current_app.config.get("RATE_LIMIT_OAUTH_CALLBACK_MAX", 10)
    window = current_app.config.get("RATE_LIMIT_OAUTH_CALLBACK_WINDOW", 300)
    key = _callback_limit_key()
    if ratelimit.count(key, window=window) >= max_hits:
        return True, ratelimit.retry_after(key, window=window)
    return False, 0


def _throttled_response(retry_after: int):
    """Return a ``429`` response (with ``Retry-After``) for a throttled callback."""
    response = current_app.response_class(
        "Too many failed GitHub authorization attempts. Please try again later.",
        status=429,
        mimetype="text/plain",
    )
    response.headers["Retry-After"] = str(retry_after)
    return response


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@bp.route("/repos")
@login_required
def repos():
    """Repository browser page."""
    return render_template("github/repos.html")


@bp.route("/repos/<owner>/<repo>")
@login_required
def repo_detail(owner: str, repo: str):
    """Repository detail page."""
    return render_template("github/repo_detail.html", owner=owner, repo=repo)


@bp.route("/repos/<owner>/<repo>/commits/<sha>")
@login_required
def commit_detail(owner: str, repo: str, sha: str):
    """Single commit page with the full message and per-file diffs."""
    return render_template("github/commit_detail.html", owner=owner, repo=repo, sha=sha)


@bp.route("/repos/<owner>/<repo>/issues")
@login_required
def issues(owner: str, repo: str):
    """Issue list page."""
    return render_template("github/issues.html", owner=owner, repo=repo)


@bp.route("/repos/<owner>/<repo>/issues/<int:number>")
@login_required
def issue_detail(owner: str, repo: str, number: int):