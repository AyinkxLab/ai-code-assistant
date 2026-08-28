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

from flask import Flask, render_template

from app.config import config_by_name
from app.extensions import csrf, db, login_manager, migrate


def create_app(config_name: str | None = None) -> Flask:
    """Build and configure the Flask application.

    :param config_name: Key into :data:`app.config.config_by_name`.
        Defaults to the ``APP_ENV`` environment variable or ``development``.
    """
    if config_name is None:
        config_name = os.getenv("APP_ENV", "development")

    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    # Initialize extensions with the application.
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Register blueprints.
    from app.auth import bp as auth_bp
    from app.chat import bp as chat_bp
    from app.collaboration import bp as collaboration_bp
    from app.github import bp as github_bp
    from app.main import bp as main_bp
    from app.plugins import bp as plugins_bp
    from app.prompts import bp as prompts_bp
    from app.reviews import bp as reviews_bp
    from app.stellar import bp as stellar_bp
    from app.tools import bp as tools_bp
    from app.workspaces import bp as workspaces_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(prompts_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(github_bp)
    app.register_blueprint(plugins_bp)
    app.register_blueprint(workspaces_bp)
    app.register_blueprint(collaboration_bp)
    app.register_blueprint(reviews_bp)
    app.register_blueprint(stellar_bp)

    # Make the current time available to every template as ``now``.
    @app.context_processor
    def inject_template_globals():
        return {"now": datetime.now()}

    # Error handlers.
    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_error(_error):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("errors/403.html"), 403

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

    return app
