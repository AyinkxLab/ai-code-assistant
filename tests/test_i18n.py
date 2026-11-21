"""Tests for locale selection, catalogs, and RTL support (#714, #715)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "app" / "templates"
TRANSLATIONS = REPO_ROOT / "app" / "translations"
STATIC = REPO_ROOT / "app" / "static"


def _catalogs() -> dict[str, dict[str, str]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in TRANSLATIONS.glob("*.json")
    }


def test_catalogs_have_matching_keys():
    catalogs = _catalogs()
    english = set(catalogs["en"])
    for locale, catalog in catalogs.items():
        assert set(catalog) == english, locale


def test_arabic_locale_sets_rtl_and_translates(client):
    html = client.get("/?lang=ar").get_data(as_text=True)
    assert '<html lang="ar" dir="rtl">' in html
    assert "الرئيسية" in html
    assert "css/rtl.css" in html


def test_unknown_locale_falls_back_to_english(client):
    html = client.get("/?lang=zz").get_data(as_text=True)
    assert '<html lang="en" dir="ltr">' in html
    assert "css/rtl.css" not in html


def test_locale_choice_persists_in_session(client):
    assert client.get("/i18n/locale/ar").status_code == 302
    html = client.get("/").get_data(as_text=True)
    assert 'dir="rtl"' in html


def test_catalog_endpoint_and_unknown_locale(client):
    payload = client.get("/i18n/catalog/ar.json").get_json()
    assert payload["dir"] == "rtl"
    assert payload["messages"]["nav.home"] == "الرئيسية"
    assert client.get("/i18n/catalog/zz.json").status_code == 404


def test_templates_and_javascript_share_one_catalog():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    script = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    assert 'id="i18n-catalog"' in base
    assert "i18n-catalog" in script
    assert "AICA_I18N" in script


def test_rtl_stylesheet_uses_logical_properties():
    css = (STATIC / "css" / "rtl.css").read_text(encoding="utf-8")
    for token in ('[dir="rtl"]', "margin-inline", "padding-inline", "margin-inline-start"):
        assert token in css


def test_string_extraction_reports_no_missing_keys():
    spec = importlib.util.spec_from_file_location(
        "extract_i18n_strings", REPO_ROOT / "scripts" / "extract_i18n_strings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.find_issues() == []
