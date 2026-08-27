"""Application configuration.

Configuration is loaded from environment variables so the same codebase can
run locally, in CI, and in production without modification. Sensitive values
such as the database password and secret key must never be committed to the
repository; supply them through environment variables or a local ``.env``
file (see ``.env.example``).
"""

import os
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv
from sqlalchemy.pool import StaticPool

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from a .env file if one exists. This is a
# convenience for local development only; production deployments should
# provide variables via the container/platform environment.
load_dotenv(BASE_DIR / ".env")


def _db_uri() -> str:
    """Return the SQLAlchemy database URI for the current environment.

    Defaults to a local SQLite file so the application is runnable with zero
    configuration for development, while still being PostgreSQL-first in
    production (see ``docker-compose.yml``).

    For file-backed SQLite databases the parent directory is created
    automatically (SQLAlchemy does not create parent folders itself).
    """
    uri = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'instance' / 'app.db'}",
    )
    if uri.startswith("sqlite:///") and "sqlite:///:memory:" not in uri:
        db_file = Path(uri.replace("sqlite:///", "", 1))
        db_file.parent.mkdir(parents=True, exist_ok=True)
    return uri


class Config:
    """Base configuration shared by all environments."""

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = _db_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Session lifetime in seconds (12 hours).
    PERMANENT_SESSION_LIFETIME = int(os.getenv("SESSION_LIFETIME", 60 * 60 * 12))

    # App branding / feature toggles.
    APP_NAME = os.getenv("APP_NAME", "AI Code Assistant")
    SESSION_COOKIE_SECURE = False

    # Maximum size of an uploaded file in bytes (configured for future phases).
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))

    # LLM provider backend: "mock" (default, offline) or "openai".
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")

    # GitHub OAuth integration.
    GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
    GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
    GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI", "")
    GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
    GITHUB_SCOPES = os.getenv("GITHUB_SCOPES", "read:user repo")
    GITHUB_REQUEST_TIMEOUT = int(os.getenv("GITHUB_REQUEST_TIMEOUT", "30"))
    GITHUB_MAX_CONTEXT_CHARS = int(os.getenv("GITHUB_MAX_CONTEXT_CHARS", "40000"))

    # Project workspaces (Phase 5): limits that protect the server from being
    # overwhelmed by large or malicious project imports. Archives are validated
    # during extraction (path traversal, symlinks, size and file-count caps) and
    # only bounded, sanitized metadata + text content is stored.
    PROJECT_MAX_ARCHIVE_BYTES = int(os.getenv("PROJECT_MAX_ARCHIVE_BYTES", str(50 * 1024 * 1024)))
    PROJECT_MAX_SIZE_BYTES = int(os.getenv("PROJECT_MAX_SIZE_BYTES", str(500 * 1024 * 1024)))
    PROJECT_MAX_FILE_COUNT = int(os.getenv("PROJECT_MAX_FILE_COUNT", "20000"))
    PROJECT_MAX_FILE_CHARS = int(os.getenv("PROJECT_MAX_FILE_CHARS", "200000"))
    PROJECT_MAX_CONTEXT_CHARS = int(os.getenv("PROJECT_MAX_CONTEXT_CHARS", "40000"))
    PROJECT_SEARCH_MAX_RESULTS = int(os.getenv("PROJECT_SEARCH_MAX_RESULTS", "100"))
    PROJECT_GITHUB_MAX_FILES = int(os.getenv("PROJECT_GITHUB_MAX_FILES", "1000"))
    PROJECT_SKIP_DIRS = os.getenv(
        "PROJECT_SKIP_DIRS",
        ".git,.hg,.svn,node_modules,.venv,venv,__pycache__,.next,.cache,dist,build,"
        "vendor,.tox,.mypy_cache,.pytest_cache",
    )
    PROJECT_SKIP_SECRET_FILES = os.getenv(
        "PROJECT_SKIP_SECRET_FILES",
        ".env,.pem,.key,.p12,.pfx,id_rsa,id_ed25519,id_dsa,credentials,.htpasswd,"
        ".npmrc,.pypirc,secrets.yaml,secret.yaml,secret.yml",
    )

    # AI reviews (Phase 6): caps that keep reviews bounded and predictable.
    # A review never sends more than REVIEW_MAX_CONTEXT_CHARS of repository text
    # to the model, and never analyzes more than REVIEW_MAX_FILES changed files.
    REVIEW_MAX_FILES = int(os.getenv("REVIEW_MAX_FILES", "40"))
    REVIEW_MAX_CONTEXT_CHARS = int(os.getenv("REVIEW_MAX_CONTEXT_CHARS", "40000"))
    REVIEW_MAX_FINDINGS = int(os.getenv("REVIEW_MAX_FINDINGS", "100"))
    # Default enabled project review kinds (comma-separated).
    REVIEW_KINDS = os.getenv("REVIEW_KINDS", "quality,security,tests")
    # Only findings at or above this severity are stored: critical|high|medium|low|informational.
    REVIEW_SEVERITY_THRESHOLD = os.getenv("REVIEW_SEVERITY_THRESHOLD", "low")

    # Team collaboration (Phase 7).
    # Invitation default time-to-live in hours (168 = 7 days).
    INVITE_TTL_HOURS = int(os.getenv("INVITE_TTL_HOURS", "168"))
    # Maximum workspace members included in AI team context (per-project chat).
    PROJECT_MAX_MEMBER_CONTEXT = int(os.getenv("PROJECT_MAX_MEMBER_CONTEXT", "20"))
    # In-memory rate limiting for public collaboration endpoints (accept/
    # decline/landing) and the presence heartbeat. Simple sliding-window limiter
    # in app/services/ratelimit.py; broader per-user AI/import limits are the
    # scope of the umbrella issues (#28/#81/#106).
    RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
    RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "300"))
    # Optional SMTP for invitation email delivery. When unset, invitations are
    # delivered as in-app notifications only and the app never crashes on mail.
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "")
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "1") == "1"

    # Stellar / Soroban integration (Phase 8).
    # Default network the Stellar service points at. One of: mainnet, testnet,
    # futurenet, custom. Everything defaults to testnet so the foundation is
    # safe by default (no real XLM involved).
    STELLAR_NETWORK = os.getenv("STELLAR_NETWORK", "testnet")
    # Explicit Horizon / Soroban RPC endpoints. When unset the built-in presets
    # for STELLAR_NETWORK are used (see app/services/stellar.py). These can be
    # pointed at a local `stellar-core`/`soroban-rpc` during development.
    STELLAR_HORIZON_URL = os.getenv("STELLAR_HORIZON_URL", "")
    STELLAR_RPC_URL = os.getenv("STELLAR_RPC_URL", "")
    # Request timeout in seconds for outbound Stellar network calls. A short,
    # fixed timeout keeps SSRF-style probing and slow endpoints bounded.
    STELLAR_REQUEST_TIMEOUT = int(os.getenv("STELLAR_REQUEST_TIMEOUT", "15"))
    # Cap on bytes read from a Stellar network response body.
    STELLAR_MAX_RESPONSE_BYTES = int(os.getenv("STELLAR_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))


class DevelopmentConfig(Config):
    """Local development configuration."""

    DEBUG = True
    SESSION_COOKIE_SECURE = False


class TestingConfig(Config):
    """Configuration used by the automated test suite.

    Uses an in-memory SQLite database and disables CSRF so that test clients
    do not need to fetch and submit a token for every request.
    """

    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    # Keep a single in-memory SQLite connection alive across the test run.
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict] = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }


class ProductionConfig(Config):
    """Production configuration.

    Requires explicit configuration of the secret key and database URL. Fails
    fast on startup if required settings are missing rather than silently
    running with insecure defaults.
    """

    DEBUG = False

    def __init__(self) -> None:
        if not os.getenv("SECRET_KEY"):
            raise RuntimeError("SECRET_KEY must be set in the production environment.")
        if not os.getenv("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be set in the production environment.")
        if not os.getenv("DATABASE_URL", "").startswith("postgresql"):
            raise RuntimeError(
                "Production must use PostgreSQL (DATABASE_URL starting with " "postgresql://)."
            )

    # Secure session cookie over HTTPS.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"


# Registry used by the application factory via ``create_app(config_name)``.
config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
