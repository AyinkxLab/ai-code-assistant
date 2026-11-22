"""Authentication routes: register, login, and logout.

Phase 1 provides the scaffolding for account management. Future phases will
extend this blueprint with email verification, password reset, profile
management, and session hardening.
"""

import json
from functools import wraps
from urllib.parse import urljoin, urlparse

from flask import (
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.api.error_catalog import (
    CONFLICT,
    FORBIDDEN,
    GONE,
    INVALID_INPUT,
    NOT_FOUND,
    RATE_LIMITED,
    UNAUTHORIZED,
    error_response,
)
from app.auth import bp
from app.extensions import db
from app.models import User, UserDataExport, UserDeletionRequest
from app.services import audit, chat_audit, data_deletion, data_export, domain_verification, mfa


def _api_login_required(view):
    """Reject unauthenticated JSON API calls with the catalog 401 shape."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return error_response(UNAUTHORIZED)
        return view(*args, **kwargs)

    return wrapper


def _domain_error_response(exc: Exception):
    """Map a domain-verification failure to a structured catalog response."""
    if isinstance(exc, domain_verification.DomainPermissionDenied):
        code = FORBIDDEN
    elif isinstance(exc, domain_verification.ChallengeExpired):
        code = GONE
    elif isinstance(
        exc, (domain_verification.DomainConflict, domain_verification.DomainNotVerified)
    ):
        code = CONFLICT
    elif isinstance(exc, LookupError):
        code = NOT_FOUND
    else:
        code = INVALID_INPUT
    db.session.rollback()
    return error_response(code, str(exc))


def _deletion_error_response(exc: Exception):
    """Map a deletion-workflow failure to a structured catalog response."""
    if isinstance(exc, data_deletion.RateLimited):
        response = error_response(RATE_LIMITED, str(exc))
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    if isinstance(
        exc,
        (
            data_deletion.AlreadyCompleted,
            data_deletion.DeletionConflict,
        ),
    ):
        code = CONFLICT
    elif isinstance(exc, data_deletion.ConfirmationMismatch):
        code = INVALID_INPUT
    elif isinstance(exc, LookupError):
        code = NOT_FOUND
    else:
        code = INVALID_INPUT
    if not isinstance(exc, data_deletion.RateLimited):
        db.session.rollback()
    return error_response(code, str(exc))


def _domain_payload(data: dict | None) -> tuple[int, str] | None:
    """Return ``(workspace_id, domain)`` from a JSON body, or ``None``."""
    if not isinstance(data, dict):
        return None
    try:
        workspace_id = int(data.get("workspace_id"))
    except (TypeError, ValueError):
        return None
    domain = data.get("domain")
    if not isinstance(domain, str):
        return None
    return workspace_id, domain


def _is_safe_redirect_target(target: str) -> bool:
    """Return ``True`` if ``target`` is a safe internal redirect URL.

    Prevents open-redirect attacks where a ``next`` query parameter points to
    an external host.
    """
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in {"http", "https"} and host_url.netloc == redirect_url.netloc


@bp.before_app_request
def _revoke_deprovisioned_sessions():
    """Drop a session whose account was deactivated after login (#639).

    Deprovisioning an account (``User.is_active = False``) must not leave an
    already-authenticated browser session usable. Flask-Login reloads the user
    from the database on each request, so checking here revokes the existing
    session on its next request without a server-side session store.
    """
    if current_user.is_authenticated and not current_user.is_active:
        logout_user()


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Create a new user account.

    Validates uniqueness of username and email, hashes the password, and logs
    the new user straight in. Email verification will be added in a later
    phase.
    """
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    error = None
    next_url = request.form.get("next") or request.args.get("next")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not username or not email or not password:
            error = "All fields are required."
        elif len(username) < 3 or len(username) > 80:
            error = "Username must be between 3 and 80 characters."
        elif "@" not in email or "." not in email:
            error = "Please enter a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters long."
        elif password != password_confirm:
            error = "Passwords do not match."
        elif User.query.filter_by(username=username).first():
            error = "That username is already taken."
        elif User.query.filter_by(email=email).first():
            error = "An account with that email already exists."

        if error is None:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            audit.record(
                audit.USER_CREATED,
                user=user,
                target_type="user",
                target_id=user.id,
                metadata={"username": user.username, "email": user.email},
            )
            db.session.commit()
            chat_audit.log_event("chat.register", user_id=user.id, status="success")
            login_user(user)
            flash("Welcome! Your account was created successfully.", "success")
            if next_url and _is_safe_redirect_target(next_url):
                return redirect(next_url)
            return redirect(url_for("main.index"))

    return render_template("auth/register.html", error=error, next_url=next_url or "")


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate an existing user against the stored password hash."""
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    error = None
    next_url = request.form.get("next") or request.args.get("next")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"

        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            error = "Invalid email or password."
            audit.record(audit.LOGIN_FAILURE, metadata={"email": email})
            db.session.commit()
            # Structured log: identifiers/status only, never the email or password.
            chat_audit.log_event(
                "chat.login",
                user_id=(user.id if user is not None else None),
                status="failure",
                reason="invalid_credentials",
            )
        elif domain_verification.is_email_domain_enforced(user.email):
            error = (
                "Password sign-in is disabled for your organization. "
                "Use single sign-on (SSO) to continue."
            )
            audit.record(
                audit.LOGIN_FAILURE,
                user=user,
                target_type="user",
                target_id=user.id,
                metadata={"email": email, "reason": "sso_enforced"},
            )
            db.session.commit()
            chat_audit.log_event(
                "chat.login", user_id=user.id, status="failure", reason="sso_enforced"
            )
        elif not user.is_active:
            error = "This account has been disabled. Contact support."
            audit.record(
                audit.LOGIN_FAILURE,
                user=user,
                target_type="user",
                target_id=user.id,
                metadata={"email": email, "reason": "inactive"},
            )
            db.session.commit()
            chat_audit.log_event("chat.login", user_id=user.id, status="failure", reason="inactive")
        elif mfa.mfa_required_for(user) and not user.mfa_enabled:
            error = (
                "Multi-factor authentication is required by your organization. "
                "Enroll an authenticator app before signing in, or contact an "
                "administrator for a break-glass recovery."
            )
            audit.record(
                audit.LOGIN_FAILURE,
                user=user,
                target_type="user",
                target_id=user.id,
                metadata={"email": email, "reason": "mfa_required"},
            )
            db.session.commit()
            chat_audit.log_event(
                "chat.login", user_id=user.id, status="failure", reason="mfa_required"
            )
        elif user.mfa_enabled:
            session["mfa_pending_user_id"] = user.id
            session["mfa_pending_remember"] = remember
            session["mfa_pending_next"] = next_url or ""
            return redirect(url_for("auth.mfa_challenge"))
        else:
            user.touch_last_login()
            audit.record(
                audit.LOGIN_SUCCESS,
                user=user,
                target_type="user",
                target_id=user.id,
                metadata={"email": user.email},
            )
            db.session.commit()
            chat_audit.log_event("chat.login", user_id=user.id, status="success")
            login_user(user, remember=remember)
            flash(f"Welcome back, {user.username}!", "success")

            if next_url and _is_safe_redirect_target(next_url):
                return redirect(next_url)
            return redirect(url_for("main.index"))

    return render_template("auth/login.html", error=error, next_url=next_url or "")


@bp.route("/mfa", methods=["GET", "POST"])
def mfa_challenge():
    """Second login step for users with MFA enabled (#618)."""
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    user_id = session.get("mfa_pending_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))
    user = db.session.get(User, int(user_id))
    if user is None or not user.mfa_enabled:
        session.pop("mfa_pending_user_id", None)
        return redirect(url_for("auth.login"))

    error = None
    if request.method == "POST":
        if mfa.verify_code(user.mfa_secret, request.form.get("code", "")):
            remember = bool(session.pop("mfa_pending_remember", False))
            next_url = session.pop("mfa_pending_next", "")
            session.pop("mfa_pending_user_id", None)
            user.touch_last_login()
            audit.record(
                audit.LOGIN_SUCCESS,
                user=user,
                target_type="user",
                target_id=user.id,
                metadata={"email": user.email, "mfa": True},
            )
            db.session.commit()
            chat_audit.log_event("chat.login", user_id=user.id, status="success", reason="mfa")
            login_user(user, remember=remember)
            flash(f"Welcome back, {user.username}!", "success")
            if next_url and _is_safe_redirect_target(next_url):
                return redirect(next_url)
            return redirect(url_for("main.index"))
        error = "Invalid authentication code."
        chat_audit.log_event("chat.login", user_id=user.id, status="failure", reason="mfa_invalid")

    return render_template("auth/mfa.html", error=error)


@bp.route("/mfa/setup", methods=["GET", "POST"])
@login_required
def mfa_setup():
    """Enroll the current user in TOTP MFA."""
    secret = session.get("mfa_setup_secret")
    if not secret:
        secret = mfa.generate_secret()
        session["mfa_setup_secret"] = secret

    error = None
    if request.method == "POST":
        if mfa.verify_code(secret, request.form.get("code", "")):
            current_user.mfa_secret = secret
            current_user.mfa_enabled = True
            db.session.commit()
            session.pop("mfa_setup_secret", None)
            flash("Multi-factor authentication enabled.", "success")
            return redirect(url_for("auth.me"))
        error = "Invalid authentication code. Try the current code."

    return render_template(
        "auth/mfa_setup.html",
        secret=secret,
        uri=mfa.provisioning_uri(current_user.email, secret),
        error=error,
    )


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """End the current user's session."""
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/me")
@login_required
def me():
    """Simple authenticated endpoint used to verify auth is working."""
    return render_template("auth/me.html")


@bp.route("/api/domains", methods=["GET"])
@_api_login_required
def list_domains():
    """List the workspace's verified and pending domains (owner only)."""
    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id is None:
        return error_response(INVALID_INPUT, "workspace_id must be an integer.")
    try:
        rows = domain_verification.list_domains(workspace_id)
    except Exception as exc:
        return _domain_error_response(exc)
    return jsonify({"items": [row.to_dict() for row in rows]})


@bp.route("/api/domains/challenge", methods=["POST"])
@_api_login_required
def create_domain_challenge():
    """Create or refresh a bounded DNS TXT ownership challenge."""
    parsed = _domain_payload(request.get_json(silent=True))
    if parsed is None:
        return error_response(
            INVALID_INPUT, "workspace_id (integer) and domain (string) are required."
        )
    workspace_id, domain = parsed
    try:
        row = domain_verification.create_challenge(workspace_id, domain)
    except Exception as exc:
        return _domain_error_response(exc)

    audit.record(
        audit.DOMAIN_CHALLENGE_CREATED,
        workspace_id=workspace_id,
        user=current_user,
        target_type="workspace_domain",
        target_id=row.id,
        metadata={"domain": row.domain},
    )
    db.session.commit()
    payload = row.to_dict()
    payload["txt_record_name"] = domain_verification.txt_record_name(row.domain)
    payload["txt_record_value"] = domain_verification.expected_txt_value(row.verification_token)
    return jsonify(payload), 201


@bp.route("/api/domains/verify", methods=["POST"])
@_api_login_required
def verify_domain():
    """Verify a challenge by reading the domain's DNS TXT record."""
    parsed = _domain_payload(request.get_json(silent=True))
    if parsed is None:
        return error_response(
            INVALID_INPUT, "workspace_id (integer) and domain (string) are required."
        )
    workspace_id, domain = parsed
    try:
        row = domain_verification.verify_domain(workspace_id, domain)
    except Exception as exc:
        return _domain_error_response(exc)

    audit.record(
        audit.DOMAIN_VERIFIED,
        workspace_id=workspace_id,
        user=current_user,
        target_type="workspace_domain",
        target_id=row.id,
        metadata={"domain": row.domain},
    )
    db.session.commit()
    return jsonify(row.to_dict())


@bp.route("/api/domains/<int:domain_id>", methods=["PATCH"])
@_api_login_required
def update_domain(domain_id: int):
    """Enable or disable SSO enforcement for a verified domain."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("enforce_sso"), bool):
        return error_response(INVALID_INPUT, "enforce_sso must be a boolean.")
    try:
        workspace_id = int(data.get("workspace_id"))
    except (TypeError, ValueError):
        return error_response(INVALID_INPUT, "workspace_id must be an integer.")
    try:
        row = domain_verification.set_enforcement(workspace_id, domain_id, data["enforce_sso"])
    except Exception as exc:
        return _domain_error_response(exc)

    audit.record(
        audit.DOMAIN_SSO_ENFORCEMENT_CHANGED,
        workspace_id=workspace_id,
        user=current_user,
        target_type="workspace_domain",
        target_id=row.id,
        metadata={"domain": row.domain, "enforce_sso": row.enforce_sso},
    )
    db.session.commit()
    return jsonify(row.to_dict())


@bp.route("/api/data-export", methods=["GET", "POST"])
@login_required
def data_export_requests():
    """List or create the current user's account-data export requests (#614).

    A new request is rate limited, processed, and answered with a one-time
    download token. The token is never stored in plaintext.
    """
    if request.method == "GET":
        return jsonify(
            {"items": [row.to_dict() for row in data_export.list_requests(current_user)]}
        )

    try:
        row = data_export.request_export(current_user)
        row, token = data_export.process_request(row.id)
    except data_export.RateLimited as exc:
        response = error_response(RATE_LIMITED, str(exc))
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except (ValueError, LookupError) as exc:
        db.session.rollback()
        return error_response(INVALID_INPUT, str(exc))

    payload = row.to_dict()
    payload["download_token"] = token
    payload["download_path"] = f"/auth/api/data-export/{row.id}/download"
    return jsonify(payload), 201


@bp.route("/api/data-export/<int:request_id>", methods=["GET"])
@login_required
def data_export_request(request_id: int):
    """Return one of the current user's export requests."""
    row = db.session.get(UserDataExport, request_id)
    if row is None or row.user_id != current_user.id:
        return error_response(NOT_FOUND)
    return jsonify(row.to_dict())


@bp.route("/api/data-export/<int:request_id>/download", methods=["GET"])
@login_required
def data_export_download(request_id: int):
    """Download an export through its one-time, expiring token."""
    token = (request.args.get("token") or "").strip()
    if not token:
        return error_response(INVALID_INPUT, "A download token is required.")
    try:
        payload = data_export.download_export(request_id, token, current_user)
    except data_export.ExportExpired as exc:
        db.session.rollback()
        return error_response(GONE, str(exc))
    except data_export.ExportAlreadyDownloaded as exc:
        db.session.rollback()
        return error_response(CONFLICT, str(exc))
    except LookupError:
        db.session.rollback()
        return error_response(NOT_FOUND)
    except (ValueError, data_export.DataExportError) as exc:
        db.session.rollback()
        return error_response(CONFLICT, str(exc))

    response = Response(json.dumps(payload, indent=2), mimetype="application/json")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="account-data-export-{request_id}.json"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/data-deletion", methods=["GET", "POST"])
@login_required
def data_deletion_requests():
    """List or create the current user's deletion/anonymization requests (#615)."""
    if request.method == "GET":
        return jsonify(
            {"items": [row.to_dict() for row in data_deletion.list_requests(current_user)]}
        )

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return error_response(INVALID_INPUT, "The request body must be a JSON object.")
    mode = data.get("mode") or data_deletion.MODE_DELETION
    try:
        row = data_deletion.request_deletion(current_user, mode=mode)
    except (data_deletion.DeletionError, ValueError) as exc:
        return _deletion_error_response(exc)
    return jsonify(row.to_dict()), 201


@bp.route("/api/data-deletion/<int:request_id>", methods=["GET"])
@login_required
def data_deletion_request(request_id: int):
    """Return one of the current user's deletion requests."""
    row = db.session.get(UserDeletionRequest, request_id)
    if row is None or row.user_id != current_user.id:
        return error_response(NOT_FOUND)
    return jsonify(row.to_dict())


@bp.route("/api/data-deletion/<int:request_id>/confirm", methods=["POST"])
@login_required
def data_deletion_confirm(request_id: int):
    """Confirm and apply a deletion request after the exact phrase is supplied."""
    data = request.get_json(silent=True) or request.form
    confirmation = data.get("confirmation") if data else None
    try:
        row, summary = data_deletion.confirm_request(request_id, current_user, confirmation)
    except (data_deletion.DeletionError, ValueError, LookupError) as exc:
        return _deletion_error_response(exc)

    logout_user()
    payload = row.to_dict()
    payload["summary"] = summary
    return jsonify(payload)


@bp.route("/api/data-deletion/<int:request_id>/cancel", methods=["POST"])
@login_required
def data_deletion_cancel(request_id: int):
    """Cancel a pending deletion request without touching account data."""
    try:
        row = data_deletion.cancel_request(request_id, current_user)
    except (data_deletion.DeletionError, ValueError, LookupError) as exc:
        return _deletion_error_response(exc)
    return jsonify(row.to_dict())
