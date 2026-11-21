"""Lightweight translation framework with locale selection and RTL support.

Issues #714 and #715: user-facing strings live in JSON catalogs under
``app/translations``. The active locale comes from ``?lang=``, then the session,
then ``Accept-Language``, falling back to English. Missing keys fall back to
English and then to the key itself, so a partial catalog never renders blank.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from flask import Blueprint, g, request, session

DEFAULT_LOCALE = "en"
#: Supported locales with their display label and writing direction.
SUPPORTED_LOCALES: dict[str, dict[str, str]] = {
    "en": {"label": "English", "dir": "ltr"},
    "ar": {"label": "العربية", "dir": "rtl"},
}

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"

bp = Blueprint("i18n", __name__, url_prefix="/i18n")


@cache
def load_catalog(locale: str) -> dict[str, str]:
    """Load a locale catalog, returning an empty mapping when absent."""
    path = TRANSLATIONS_DIR / f"{locale}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def normalize_locale(value: str | None) -> str | None:
    """Return a supported base locale (``ar-EG`` -> ``ar``), or ``None``."""
    if not value:
        return None
    base = value.strip().lower().replace("_", "-").split("-")[0]
    return base if base in SUPPORTED_LOCALES else None


def get_locale() -> str:
    """Return the locale resolved for the current request."""
    return getattr(g, "locale", DEFAULT_LOCALE)


def text_direction(locale: str | None = None) -> str:
    """Return ``ltr`` or ``rtl`` for a locale."""
    resolved = locale or get_locale()
    return SUPPORTED_LOCALES.get(resolved, SUPPORTED_LOCALES[DEFAULT_LOCALE])["dir"]


def resolve_locale() -> str:
    """Resolve the request locale with query, session, and header fallbacks."""
    for candidate in (
        normalize_locale(request.args.get("lang")),
        normalize_locale(session.get("locale")),
        normalize_locale(request.accept_languages.best_match(list(SUPPORTED_LOCALES))),
    ):
        if candidate:
            return candidate
    return DEFAULT_LOCALE


def translate(key: str, locale: str | None = None, **values: object) -> str:
    """Translate ``key`` with English fallback, then the key itself."""
    resolved = locale or get_locale()
    template = load_catalog(resolved).get(key) or load_catalog(DEFAULT_LOCALE).get(key) or key
    try:
        return template.format(**values) if values else template
    except (KeyError, IndexError, ValueError):
        return template


def t(key: str, **values: object) -> str:
    """Translate ``key`` for the current request locale."""
    return translate(key, get_locale(), **values)


def init_i18n(app) -> None:
    """Install locale resolution, template helpers, and response headers."""

    @app.before_request
    def _select_locale() -> None:
        requested = normalize_locale(request.args.get("lang"))
        if requested:
            session["locale"] = requested
        g.locale = resolve_locale()

    @app.after_request
    def _locale_headers(response):
        response.headers["Content-Language"] = get_locale()
        vary = response.headers.get("Vary")
        response.headers["Vary"] = "Accept-Language" if not vary else f"{vary}, Accept-Language"
        return response

    @app.context_processor
    def _inject_i18n() -> dict:
        locale = get_locale()
        return {
            "t": t,
            "current_locale": locale,
            "text_direction": text_direction(locale),
            "supported_locales": SUPPORTED_LOCALES,
            "i18n_catalog": load_catalog(locale),
        }


from app.i18n import routes  # noqa: E402,F401  (register routes on import)
