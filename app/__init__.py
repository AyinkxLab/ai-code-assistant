"""Application factory.

Creates and configures the Flask application, registers blueprints, binds
extensions, and installs error handlers. Import as::

    from app import create_app

    app = create_app()

The factory pattern keeps the app easy to test (each test can create a fresh
instance with the ``testing`` configuration).
"""

import os
from datetime import datetime

from flask import Flask, g, render_template, request

from app.config import config_by_name
from app.extensions import csrf, db, login_manager, migrate, socketio


def create_app(config_name: str | None = None) -> Flask:
    """Build and configure the Flask application.

    :param config_name: Key into :data:`app.config.config_by_name`.
        Defaults to the ``APP_ENV`` environment variable or ``development``.
    """
    if config_name is None:
        config_name = os.getenv("APP_ENV", "development")

    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    # Reject malformed plugin trust configuration at startup instead of
    # silently treating every publisher as untrusted (#386).
    from app.services.plugin_trust import PluginTrustConfigError, validate_trust_config

    try:
        validate_trust_config(app.config)
    except PluginTrustConfigError as exc:
        raise RuntimeError(f"Invalid plugin trust configuration: {exc}") from exc

    # Initialize extensions with the application.
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Real-time collaboration over WebSockets (issue #50). ``manage_session``
    # lets socket handlers reuse the Flask-Login session, so room joins are
    # authorized with the same identity as HTTP routes.
    allowed_origins = app.config.get("SOCKETIO_CORS_ALLOWED_ORIGINS") or None
    socketio.init_app(
        app,
        async_mode=app.config.get("SOCKETIO_ASYNC_MODE", "threading"),
        cors_allowed_origins=allowed_origins,
        manage_session=True,
    )

    # Structured, security-relevant chat logging + request-id correlation (#17).
    from app.services import chat_audit

    chat_audit.init_app(app)

    # Per-user LLM response cache (issue #18).
    from app.services.llm_cache import configure_cache

    configure_cache(app)

    # Per-account GitHub GET response cache with ETag revalidation (issue #76).
    from app.services.github_cache import configure_github_cache

    configure_github_cache(app)

    # Bounded cache for hot, non-secret reads (#698).
    from app.services.read_cache import configure_read_cache

    configure_read_cache(app)

    # Validate the optional LLM_PRICE_TABLE override at startup (#438). Invalid
    # configuration is logged and falls back to the shipped estimates instead of
    # breaking pricing or request handling.
    from app.services.pricing import configure_price_table

    configure_price_table(app)

    # Prometheus metrics and the protected /metrics endpoint (Phase 10, #370).
    # Registers the blueprint plus the per-request counter hook.
    from app.observability import init_observability

    init_observability(app)

    # Per-request query counts and the slow-query log (#696).
    from app.observability.query_profiling import init_query_profiling

    init_query_profiling(app)

    # Request/service/plugin spans flushed as bounded JSON log records (#703).
    from app.observability.tracing import init_tracing

    init_tracing(app)

    # Register blueprints.
    from app.admin import bp as admin_bp
    from app.api.docs import bp as api_docs_bp
    from app.auth import bp as auth_bp
    from app.billing import bp as billing_bp
    from app.chat import bp as chat_bp
    from app.chat.api import bp as chat_api_bp
    from app.collaboration import bp as collaboration_bp
    from app.github import bp as github_bp
    from app.gitlab import bp as gitlab_bp
    from app.i18n import bp as i18n_bp
    from app.i18n import init_i18n
    from app.integrations import bp as integrations_bp
    from app.keys import bp as keys_bp
    from app.main import bp as main_bp
    from app.notification_settings import bp as notification_settings_bp
    from app.plugins import bp as plugins_bp
    from app.prompts import bp as prompts_bp
    from app.reviews import bp as reviews_bp
    from app.stellar import bp as stellar_bp
    from app.tools import bp as tools_bp
    from app.webhooks import bp as webhooks_bp
    from app.workspaces import bp as workspaces_bp

    init_i18n(app)
    app.register_blueprint(i18n_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(chat_api_bp)
    app.register_blueprint(api_docs_bp)
    app.register_blueprint(prompts_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(github_bp)
    app.register_blueprint(keys_bp)
    app.register_blueprint(plugins_bp)
    app.register_blueprint(workspaces_bp)
    app.register_blueprint(collaboration_bp)
    app.register_blueprint(reviews_bp)
    app.register_blueprint(stellar_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(notification_settings_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(gitlab_bp)
    app.register_blueprint(integrations_bp)
    app.register_blueprint(admin_bp)

    # Register Socket.IO event handlers (import has side effects by design).
    from app.collaboration import realtime  # noqa: F401

    # Fan out review/security events to configured Slack/Teams/email
    # integrations (best-effort; never affects the emitting request).
    from app.services.integrations.dispatch import register_event_handlers

    register_event_handlers(app)

    @app.after_request
    def add_budget_warning_header(response):
        """Expose a non-blocking budget warning to API/UI clients."""
        decision = getattr(g, "budget_decision", None)
        if decision is not None and getattr(decision, "warned", False):
            response.headers["X-Budget-Warning"] = "approaching_limit"
        return response

    # Documented security headers and CSP (#622). The policy matches what the
    # templates actually load: same-origin scripts, Google Fonts styles/fonts,
    # and same-origin WebSocket/polling for Socket.IO. Inline styles remain an
    # explicit exception (existing templates use style attributes); there are
    # no inline event handlers or inline executable scripts.
    content_security_policy = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:"
    )

    @app.after_request
    def add_security_headers(response):
        """Apply the documented security headers to every response."""
        response.headers.setdefault("Content-Security-Policy", content_security_policy)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        # HSTS is only meaningful over HTTPS and must never be sent on plain
        # HTTP in local development.
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    # Make the current time available to every template as ``now``.
    @app.context_processor
    def inject_template_globals():
        return {"now": datetime.now()}

    # Error handlers. API requests get the stable catalog shape (#650); HTML
    # requests keep the existing rendered pages.
    from app.api.error_catalog import (
        catalog_entry,
        error_code_for_status,
        error_response,
        wants_json_error,
    )

    def _http_error(error, template: str | None = None):
        code = error_code_for_status(error.code)
        if wants_json_error():
            return error_response(code)
        if template is not None:
            return render_template(template), error.code
        entry = catalog_entry(code)
        return (
            render_template("errors/error.html", status=error.code, message=entry.message),
            error.code,
        )

    @app.errorhandler(400)
    def bad_request(error):
        return _http_error(error)

    @app.errorhandler(401)
    def unauthorized(error):
        return _http_error(error)

    @app.errorhandler(403)
    def forbidden(error):
        return _http_error(error, "errors/403.html")

    @app.errorhandler(404)
    def not_found(error):
        return _http_error(error, "errors/404.html")

    @app.errorhandler(409)
    def conflict(error):
        return _http_error(error)

    @app.errorhandler(422)
    def unprocessable(error):
        return _http_error(error)

    @app.errorhandler(429)
    def rate_limited(error):
        return _http_error(error)

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return _http_error(error, "errors/500.html")

    # CLI command to bootstrap the database in a dev environment.
    @app.cli.command("init-db")
    def init_db_command():
        """Create all database tables from the models."""
        from app.models import User  # noqa: F401  (ensure models are registered)

        db.create_all()
        print("Initialized the database.")

    # Read-only Stellar/Soroban CLI (network info, address validation, account
    # and contract inspection). Commands are bounded and never sign/submit.
    from app.services.stellar_cli import register_stellar_cli

    register_stellar_cli(app)

    # Operator-facing plugin management CLI (list/inspect/enable/disable and
    # local-only install). Reuses the plugin services; never grants capabilities.
    from app.services.plugins_cli import register_plugins_cli

    register_plugins_cli(app)

    # Usage-ledger maintenance (historical chat backfill and future retention
    # commands). The backfill is bounded and idempotent.
    from app.services.usage_cli import register_usage_cli

    register_usage_cli(app)

    # Durable background job worker/inspection commands.
    from app.services.jobs_cli import register_jobs_cli

    register_jobs_cli(app)

    # Audit hash-chain verification and retention maintenance (#606, #608).
    from app.services.audit_cli import register_audit_cli

    register_audit_cli(app)

    return app
