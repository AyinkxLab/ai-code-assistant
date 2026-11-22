"""Public API error catalog tests (#650)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from flask import abort

from app.api import error_catalog
from app.extensions import db
from app.models import User

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _catalog_codes_in_source() -> list[tuple[str, str, int]]:
    """Return ``(code, file, line)`` for every literal ``error_response`` call."""
    found: list[tuple[str, str, int]] = []
    for path in APP_DIR.rglob("*.py"):
        if path == APP_DIR / "api" / "error_catalog.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_helper = (isinstance(func, ast.Name) and func.id == "error_response") or (
                isinstance(func, ast.Attribute) and func.attr == "error_response"
            )
            if not is_helper or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((first.value, str(path), node.lineno))
    return found


def test_every_raised_public_code_is_in_the_catalog():
    offenders = [
        f"{path}:{line} uses {code!r}"
        for code, path, line in _catalog_codes_in_source()
        if code not in error_catalog.CATALOG
    ]
    assert not offenders, "Unknown public error codes: " + ", ".join(offenders)


def test_catalog_codes_and_statuses_are_unique():
    codes = [entry.code for entry in error_catalog.CATALOG.values()]
    statuses = [entry.status for entry in error_catalog.CATALOG.values()]

    assert codes == list(error_catalog.CATALOG)
    assert len(codes) == len(set(codes))
    assert len(statuses) == len(set(statuses))
    assert all(entry.description for entry in error_catalog.CATALOG.values())


def _make_admin():
    user = User(username="admin", email="admin@example.com")
    user.set_password("supersecret123")
    user.is_admin = True
    db.session.add(user)
    db.session.commit()
    return user


def test_unknown_api_route_uses_the_catalog(client):
    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["code"] == error_catalog.NOT_FOUND
    assert payload["kind"] == "not_found"


def test_invalid_input_uses_the_catalog(client, login):
    _make_admin()
    login(email="admin@example.com")

    response = client.get("/admin/usage?days=not-a-number")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == error_catalog.INVALID_INPUT
    assert payload["kind"] == "invalid"


def test_permission_failure_uses_the_catalog(client, make_user, login):
    make_user()
    login()

    response = client.get("/admin/audit")

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["code"] == error_catalog.FORBIDDEN
    assert payload["kind"] == "forbidden"


def test_unhandled_api_error_uses_the_catalog(app, client):
    @app.route("/api/test-boom")
    def _boom():
        raise RuntimeError("secret internal detail")

    app.config["PROPAGATE_EXCEPTIONS"] = False
    response = client.get("/api/test-boom")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["code"] == error_catalog.INTERNAL_ERROR
    assert "secret internal detail" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, error_catalog.INVALID_INPUT),
        (401, error_catalog.UNAUTHORIZED),
        (403, error_catalog.FORBIDDEN),
        (404, error_catalog.NOT_FOUND),
        (409, error_catalog.CONFLICT),
        (422, error_catalog.VALIDATION_FAILED),
        (429, error_catalog.RATE_LIMITED),
    ],
)
def test_every_public_status_maps_to_a_catalog_code(app, client, status, code):
    endpoint = f"/api/test-status-{status}"

    def _abort():
        abort(status)

    app.add_url_rule(endpoint, f"status_{status}", _abort)
    response = client.get(endpoint)

    assert response.status_code == status
    assert response.get_json()["code"] == code
