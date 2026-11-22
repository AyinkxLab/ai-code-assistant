"""Security header and CSP contract (#622)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "app" / "templates"

#: Inline event handlers are not allowed. If one ever has to stay, add the
#: template path here and document the reason in docs/security-headers.md.
ALLOWED_INLINE_HANDLER_TEMPLATES: set[str] = set()

_INLINE_HANDLER_RE = re.compile(
    r"\son(?:click|load|error|submit|change|input|focus|blur|mouseover|keydown|keyup)\s*=",
    re.IGNORECASE,
)


def test_common_security_headers_are_present(client):
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert "geolocation=()" in response.headers["Permissions-Policy"]


def test_csp_matches_the_documented_policy(client):
    csp = client.get("/").headers["Content-Security-Policy"]

    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "form-action 'self'" in csp
    assert "https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in csp
    assert "connect-src 'self' ws: wss:" in csp


def test_csp_never_allows_eval_or_inline_scripts(client):
    csp = client.get("/").headers["Content-Security-Policy"]

    assert "unsafe-eval" not in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp


def test_hsts_is_only_sent_over_https(client):
    plain = client.get("/")
    secure = client.get("/", base_url="https://localhost")

    assert "Strict-Transport-Security" not in plain.headers
    assert secure.headers["Strict-Transport-Security"] == ("max-age=31536000; includeSubDomains")


def test_no_template_uses_an_undocumented_inline_event_handler():
    offenders = []
    for template in TEMPLATES.rglob("*.html"):
        rel = template.relative_to(REPO_ROOT).as_posix()
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
            if _INLINE_HANDLER_RE.search(line):
                offenders.append(f"{rel}:{number}")
    undocumented = [
        offender
        for offender in offenders
        if offender.split(":", 1)[0] not in ALLOWED_INLINE_HANDLER_TEMPLATES
    ]
    assert not undocumented, "Undocumented inline event handlers: " + ", ".join(undocumented)
