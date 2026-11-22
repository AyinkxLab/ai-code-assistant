"""Authentication routes: register, login, and logout.

Phase 1 provides the scaffolding for account management. Future phases will
extend this blueprint with email verification, password reset, profile
management, and session hardening.
"""

import json
from urllib.parse import urljoin, urlparse

from flask import Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.api.error_catalog import (
    CONFLICT,
    GONE,
    INVALID_INPUT,
    NOT_FOUND,
    RATE_LIMITED,
    error_response,
)
from app.auth import bp
from app.extensions import db
from app.models import User, UserDataExport
from app.services import audit, chat_audit, data_export


def _is_safe_redirect_target(target: str) -> bool:
    """Return ``True`` if ``target`` is a safe internal redirect URL.

    Prevents open-redirect attacks where a ``next`` query parameter points to
    an external host.
    """
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in {"http", "https"} and host_url.netloc == redirect_url.netloc


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
