"""Locale selection and translation-catalog endpoints (#714)."""

from __future__ import annotations

from flask import abort, jsonify, redirect, request, session, url_for

from app.i18n import SUPPORTED_LOCALES, bp, load_catalog, normalize_locale, text_direction


def _safe_next(target: str | None) -> str:
    """Only allow same-origin relative redirects."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.index")


@bp.route("/locale/<locale>")
def set_locale(locale: str):
    """Persist a locale choice and return to the referring page."""
    normalized = normalize_locale(locale)
    if normalized is None:
        abort(404)
    session["locale"] = normalized
    return redirect(_safe_next(request.args.get("next")))


@bp.route("/catalog/<locale>.json")
def catalog(locale: str):
    """Return a locale catalog for client-side strings."""
    normalized = normalize_locale(locale)
    if normalized is None:
        abort(404)
    return jsonify(
        {
            "locale": normalized,
            "dir": text_direction(normalized),
            "messages": load_catalog(normalized),
        }
    )


@bp.route("/")
def index():
    """List the supported locales as JSON."""
    return jsonify({"default": "en", "locales": SUPPORTED_LOCALES})
