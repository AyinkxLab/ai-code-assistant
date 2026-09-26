"""Authentication routes: register, login, and logout.

Phase 1 provides the scaffolding for account management. Future phases will
extend this blueprint with email verification, password reset, profile
management, and session hardening.
"""

from urllib.parse import urljoin, urlparse

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import bp
from app.extensions import db
from app.models import User
from app.services import audit


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
