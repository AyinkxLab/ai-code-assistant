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
    /github/api/repos/.../issues            issue list
    /github/api/repos/.../issues/<n>        single issue + AI analysis
    /github/api/repos/.../pulls             pull request list
    /github/api/repos/.../pulls/<n>         single PR + AI analysis
    /github/api/repos/.../analyze-file      AI analysis of one file
"""

from urllib.parse import urlencode

import requests
from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.github import bp
from app.models import GithubAccount
from app.services import analysis
from app.services.github import (
    GITHUB_AUTHORIZE_URL,
    GITHUB_TOKEN_URL,
    GitHubClient,
    GitHubError,
    get_github_client,
    issue_payload,
    pull_request_payload,
    repo_payload,
    validate_full_name,
    validate_path,
)

# --------------------------------------------------------------------------
# OAuth connection
# --------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    """Dashboard showing the user's GitHub connection status."""
    account = GithubAccount.query.filter_by(user_id=current_user.id).first()
    return render_template("github/index.html", account=account)


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
    error = request.args.get("error")
    if error:
        flash(f"GitHub authorization failed: {error}", "error")
        return redirect(url_for("github.index"))

    state = request.args.get("state")
    if state != _get_state():
        flash("GitHub authorization failed: state mismatch.", "error")
        return redirect(url_for("github.index"))

    code = request.args.get("code")
    if not code:
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
        message = token_data.get("error_description") or token_data.get("error") or response.text
        flash(f"GitHub authorization failed: {message}", "error")
        return redirect(url_for("github.index"))

    token = token_data["access_token"]
    client = GitHubClient(token)
    try:
        user = client.get_user()
    except GitHubError as exc:
        flash(f"Could not verify your GitHub account: {exc}", "error")
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
    db.session.commit()

    from app.services.events import emit_event

    emit_event(
        "github.connected",
        data={"github_username": account.github_username},
        user_id=current_user.id,
    )

    flash(f"Connected to GitHub as @{account.github_username}.", "success")
    return redirect(url_for("github.index"))


@bp.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    """Remove the GitHub connection and its stored token."""
    account = GithubAccount.query.filter_by(user_id=current_user.id).first()
    if account is not None:
        db.session.delete(account)
        db.session.commit()
        flash("Disconnected your GitHub account.", "info")
    return redirect(url_for("github.index"))


@bp.route("/api/status")
@login_required
def status():
    """Return whether the current user has a GitHub connection."""
    account = GithubAccount.query.filter_by(user_id=current_user.id).first()
    return jsonify(
        {"connected": account is not None, "account": account.to_dict() if account else None}
    )


# --------------------------------------------------------------------------
# OAuth state handling
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


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


@bp.route("/repos/<owner>/<repo>/issues")
@login_required
def issues(owner: str, repo: str):
    """Issue list page."""
    return render_template("github/issues.html", owner=owner, repo=repo)


@bp.route("/repos/<owner>/<repo>/issues/<int:number>")
@login_required
def issue_detail(owner: str, repo: str, number: int):
    """Single issue page."""
    return render_template("github/issue_detail.html", owner=owner, repo=repo, number=number)


@bp.route("/repos/<owner>/<repo>/pulls")
@login_required
def pulls(owner: str, repo: str):
    """Pull request list page."""
    return render_template("github/pulls.html", owner=owner, repo=repo)


@bp.route("/repos/<owner>/<repo>/pulls/<int:number>")
@login_required
def pull_detail(owner: str, repo: str, number: int):
    """Single pull request page."""
    return render_template("github/pull_detail.html", owner=owner, repo=repo, number=number)


# --------------------------------------------------------------------------
# API: repositories
# --------------------------------------------------------------------------


def _client() -> GitHubClient:
    return get_github_client()


@bp.route("/api/repos")
@login_required
def api_repos():
    """List repositories visible to the connected user, optionally filtered."""
    try:
        client = _client()
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 403

    query = request.args.get("q", "").strip().lower()
    try:
        repos = client.list_repositories()
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502

    if query:
        repos = [
            r
            for r in repos
            if query in r.get("name", "").lower() or query in r.get("full_name", "").lower()
        ]
    repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    return jsonify([repo_payload(r) for r in repos])


@bp.route("/api/repos/<owner>/<repo>")
@login_required
def api_repo_detail(owner: str, repo: str):
    """Return a single repository's metadata."""
    try:
        client = _client()
        data = client.get_repository(validate_full_name(f"{owner}/{repo}"))
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 404
    payload = repo_payload(data)
    payload["readme"] = client.get_readme(data.get("full_name", f"{owner}/{repo}"))
    return jsonify(payload)


@bp.route("/api/repos/<owner>/<repo>/branches")
@login_required
def api_branches(owner: str, repo: str):
    """Return the branches of a repository."""
    full_name = validate_full_name(f"{owner}/{repo}")
    try:
        client = _client()
        data = client.list_branches(full_name)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502
    return jsonify(
        [
            {
                "name": branch.get("name"),
                "protected": bool(branch.get("protected") or False),
                "sha": (branch.get("commit") or {}).get("sha"),
            }
            for branch in data
        ]
    )


@bp.route("/api/repos/<owner>/<repo>/tree")
@login_required
def api_tree(owner: str, repo: str):
    """Return the file tree of a ref (used for the repository browser)."""
    full_name = validate_full_name(f"{owner}/{repo}")
    ref = request.args.get("ref", "").strip() or "HEAD"
    try:
        client = _client()
        tree = client.get_tree(full_name, ref, recursive=True)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502

    entries = []
    for entry in tree.get("tree", []):
        entries.append(
            {
                "path": entry.get("path"),
                "type": entry.get("type"),
                "mode": entry.get("mode"),
                "size": entry.get("size"),
            }
        )
    return jsonify(
        {
            "ref": ref,
            "truncated": bool(tree.get("truncated")),
            "entries": entries[:2000],
        }
    )


@bp.route("/api/repos/<owner>/<repo>/contents")
@login_required
def api_contents(owner: str, repo: str):
    """Return directory contents or a single file at a given path."""
    full_name = validate_full_name(f"{owner}/{repo}")
    path = validate_path(request.args.get("path", ""))
    ref = request.args.get("ref", "").strip() or None
    try:
        client = _client()
        data = client.get_contents(full_name, path, ref=ref)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502

    if isinstance(data, dict):
        # A single file: return decoded text.
        file_path = data.get("path", path)
        try:
            text = client.get_file_text(full_name, file_path, ref=ref)
        except GitHubError as exc:
            return jsonify({"error": str(exc), "kind": exc.kind}), 422
        return jsonify(
            {
                "type": "file",
                "path": file_path,
                "name": data.get("name"),
                "size": data.get("size"),
                "language": data.get("language"),
                "text": text,
            }
        )

    rows = []
    for item in data:
        rows.append(
            {
                "type": "dir" if item.get("type") == "dir" else "file",
                "name": item.get("name"),
                "path": item.get("path"),
                "size": item.get("size"),
            }
        )
    return jsonify({"type": "dir", "path": path, "entries": rows})


# --------------------------------------------------------------------------
# API: commits
# --------------------------------------------------------------------------


@bp.route("/api/repos/<owner>/<repo>/commits")
@login_required
def api_commits(owner: str, repo: str):
    """Return commit history for a ref and/or path."""
    full_name = validate_full_name(f"{owner}/{repo}")
    ref = request.args.get("ref", "").strip() or None
    path = request.args.get("path", "").strip() or None
    try:
        client = _client()
        data = client.list_commits(full_name, ref=ref, path=path)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502

    items = []
    for commit in data:
        commit_data = commit.get("commit") or {}
        author = commit_data.get("author") or {}
        items.append(
            {
                "sha": commit.get("sha"),
                "short_sha": (commit.get("sha") or "")[:7],
                "message": (commit_data.get("message") or "").splitlines()[0],
                "author": author.get("name") or (commit.get("author") or {}).get("login"),
                "date": author.get("date"),
                "html_url": commit.get("html_url"),
            }
        )
    return jsonify(items)


@bp.route("/api/repos/<owner>/<repo>/commits/<sha>")
@login_required
def api_commit_detail(owner: str, repo: str, sha: str):
    """Return a single commit with its changed files."""
    full_name = validate_full_name(f"{owner}/{repo}")
    try:
        client = _client()
        commit = client.get_commit(full_name, sha)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 404

    commit_data = commit.get("commit") or {}
    author = commit_data.get("author") or {}
    return jsonify(
        {
            "sha": commit.get("sha"),
            "short_sha": (commit.get("sha") or "")[:7],
            "message": commit_data.get("message"),
            "author": author.get("name") or (commit.get("author") or {}).get("login"),
            "date": author.get("date"),
            "html_url": commit.get("html_url"),
            "files": [
                {
                    "filename": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                    "patch": f.get("patch"),
                }
                for f in commit.get("files", [])
            ],
        }
    )


# --------------------------------------------------------------------------
# API: issues
# --------------------------------------------------------------------------


@bp.route("/api/repos/<owner>/<repo>/issues")
@login_required
def api_issues(owner: str, repo: str):
    """Return issues for a repository (pull requests excluded)."""
    full_name = validate_full_name(f"{owner}/{repo}")
    state = request.args.get("state", "open")
    if state not in ("open", "closed", "all"):
        state = "open"
    try:
        client = _client()
        data = client.list_issues(full_name, state=state)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502
    return jsonify([issue_payload(i) for i in data])


@bp.route("/api/repos/<owner>/<repo>/issues/<int:number>")
@login_required
def api_issue_detail(owner: str, repo: str, number: int):
    """Return a single issue plus an optional AI analysis."""
    full_name = validate_full_name(f"{owner}/{repo}")
    analyze = request.args.get("analyze") == "1"
    try:
        client = _client()
        data = client.get_issue(full_name, number)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 404

    payload = issue_payload(data)
    if analyze:
        payload["analysis"] = analysis.analyze_issue(payload, owner, repo)
    return jsonify(payload)


# --------------------------------------------------------------------------
# API: pull requests
# --------------------------------------------------------------------------


@bp.route("/api/repos/<owner>/<repo>/pulls")
@login_required
def api_pulls(owner: str, repo: str):
    """Return pull requests for a repository."""
    full_name = validate_full_name(f"{owner}/{repo}")
    state = request.args.get("state", "open")
    if state not in ("open", "closed", "all"):
        state = "open"
    try:
        client = _client()
        data = client.list_pull_requests(full_name, state=state)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502
    return jsonify([pull_request_payload(pr) for pr in data])


@bp.route("/api/repos/<owner>/<repo>/pulls/<int:number>")
@login_required
def api_pull_detail(owner: str, repo: str, number: int):
    """Return a single pull request with files and optional AI analysis."""
    full_name = validate_full_name(f"{owner}/{repo}")
    analyze = request.args.get("analyze") == "1"
    try:
        client = _client()
        pr = client.get_pull_request(full_name, number)
        files = client.list_pull_request_files(full_name, number)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 404

    payload = pull_request_payload(pr)
    payload["files"] = [
        {
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
            "patch": f.get("patch"),
        }
        for f in files
    ]
    if analyze:
        payload["analysis"] = analysis.analyze_pull_request(payload, files)
    return jsonify(payload)


# --------------------------------------------------------------------------
# API: AI analysis
# --------------------------------------------------------------------------


@bp.route("/api/repos/<owner>/<repo>/analyze", methods=["POST"])
@login_required
def api_analyze_repo(owner: str, repo: str):
    """Produce an AI overview of a repository from README and file names."""
    full_name = validate_full_name(f"{owner}/{repo}")
    try:
        client = _client()
        repo_data = client.get_repository(full_name)
        default_branch = repo_data.get("default_branch") or "HEAD"
        readme = client.get_readme(full_name, ref=default_branch)
        tree = client.get_tree(full_name, default_branch, recursive=True)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502

    file_list = [
        entry.get("path", "") for entry in tree.get("tree", []) if entry.get("type") == "blob"
    ]
    return jsonify(analysis.summarize_repository(owner, repo, readme, file_list))


@bp.route("/api/repos/<owner>/<repo>/analyze-file", methods=["POST"])
@login_required
def api_analyze_file(owner: str, repo: str):
    """Analyze a single file's contents with the AI provider."""
    full_name = validate_full_name(f"{owner}/{repo}")
    data = request.get_json(silent=True) or {}
    path = validate_path(data.get("path") or "")
    ref = (data.get("ref") or "").strip() or None
    question = (data.get("question") or "").strip() or None
    if not path:
        return jsonify({"error": "A file path is required."}), 400

    try:
        client = _client()
        text = client.get_file_text(full_name, path, ref=ref)
    except GitHubError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 502

    language = (path.rsplit(".", 1)[-1] if "." in path else "") or "text"
    return jsonify(analysis.analyze_file(path, language, text, question=question))
