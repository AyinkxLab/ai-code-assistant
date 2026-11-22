"""Render the public API reference from the registered OpenAPI routes (#655).

The generator is deliberately dependency-free: the OpenAPI document is built
from Flask's URL map and rendered with :mod:`html`. It is honest about
coverage: paths are discovered automatically, while request/response schemas
are only documented for the stable ``/api/v1`` resources.

Usage::

    python scripts/render_api_docs.py

Writes ``docs/openapi.json`` and ``docs/api/index.html``. Re-run it after
adding or changing a public route.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import create_app  # noqa: E402  (path bootstrap must run first)

SPEC_PATH = REPO_ROOT / "docs" / "openapi.json"
HTML_PATH = REPO_ROOT / "docs" / "api" / "index.html"

#: Paths included in the public reference. Module-private APIs (for example
#: ``/reviews/api/...``) keep their existing internal contracts and are
#: intentionally out of scope until they are promoted.
PUBLIC_PREFIXES = ("/api/", "/api")

#: Curated descriptions for the stable versioned resources.
DESCRIPTIONS = {
    "get /api/v1/projects": "List the caller's projects with pagination.",
    "get /api/v1/projects/{project_id}": "Return one project owned by the caller.",
    "get /api/v1/reviews": "List the caller's reviews, newest first, with pagination.",
    "get /api/v1/reviews/{review_id}": "Return one review and its summary.",
    "get /api/v1/usage": "Return a paginated usage ledger summary for the caller.",
    "get /api/conversations": "List the caller's chat conversations.",
    "post /api/conversations": "Create a chat conversation.",
    "get /api/usage": "Per-user token usage summary.",
}

TAGS = [
    {"name": "v1", "description": "Stable, versioned public resources."},
    {"name": "chat", "description": "Conversation and usage endpoints."},
    {"name": "docs", "description": "Documentation endpoints."},
]


def _summary(endpoint: str, path: str, method: str) -> str:
    description = DESCRIPTIONS.get(f"{method} {path}")
    if description:
        return description
    return f"{method.upper()} {path}"


def _tag(path: str) -> str:
    if path.startswith("/api/v1"):
        return "v1"
    if "docs" in path:
        return "docs"
    return "chat"


def build_spec(app) -> dict:
    """Build an OpenAPI 3.1 document from the app's registered routes."""
    paths: dict[str, dict] = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda item: str(item)):
        path = str(rule)
        if not path.startswith(PUBLIC_PREFIXES):
            continue
        if rule.endpoint == "static":
            continue
        for method in sorted(set(rule.methods) - {"HEAD", "OPTIONS"}):
            method_lower = method.lower()
            operation = {
                "summary": _summary(rule.endpoint, path, method_lower),
                "operationId": f"{rule.endpoint}_{method_lower}".replace(".", "_"),
                "tags": [_tag(path)],
                "responses": {
                    "200": {"description": "Successful response"},
                    "400": {"description": "Structured invalid-input error"},
                    "401": {"description": "Authentication required"},
                    "403": {"description": "Permission denied"},
                    "404": {"description": "Resource not found"},
                },
            }
            paths.setdefault(path, {})[method_lower] = operation

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "AI Code Assistant public API",
            "version": "1.0.0",
            "description": (
                "Machine-facing HTTP API. Paths are generated from the registered "
                "routes; detailed request and response schemas are documented for "
                "the /api/v1 resources only."
            ),
        },
        "servers": [{"url": "/"}],
        "tags": TAGS,
        "paths": paths,
    }


def _render_endpoints(paths: dict) -> str:
    rows = []
    for path in sorted(paths):
        for method, operation in sorted(paths[path].items()):
            rows.append(
                "<tr>"
                f"<td><code>{html.escape(method.upper())}</code></td>"
                f"<td><code>{html.escape(path)}</code></td>"
                f"<td>{html.escape(operation['summary'])}</td>"
                "</tr>"
            )
    return "\n".join(rows)


def render_html(spec: dict) -> str:
    """Render the whole reference as one self-contained HTML page."""
    endpoint_rows = _render_endpoints(spec["paths"])
    endpoint_count = sum(len(operations) for operations in spec["paths"].values())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Code Assistant API reference</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 16px/1.6 system-ui, sans-serif; margin: 0; color: #1f2328; background: #fff; }}
body.dark {{ background: #0d1117; color: #e6edf3; }}
header {{ background: #0d1117; color: #fff; padding: 2rem 1rem; }}
main {{ max-width: 960px; margin: 0 auto; padding: 1rem; }}
h1 {{ margin: 0; font-size: 1.9rem; }}
h2 {{ margin-top: 2rem; border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: .95rem; }}
th, td {{ text-align: left; border-bottom: 1px solid #d0d7de;
  padding: .45rem .6rem; vertical-align: top; }}
code {{ font-family: ui-monospace, monospace; font-size: .9em; }}
.note {{ background: #f6f8fa; border-left: 4px solid #0969da; padding: .7rem 1rem; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #0d1117; color: #e6edf3; }}
  h2 {{ border-color: #30363d; }}
  th, td {{ border-color: #30363d; }}
  .note {{ background: #161b22; }}
}}
</style>
</head>
<body>
<header><h1>AI Code Assistant API reference</h1></header>
<main>
<p>This page is generated from an OpenAPI 3.1 document. It covers
<strong>{endpoint_count}</strong> operations across <strong>{len(spec["paths"])}</strong>
public paths. Paths are discovered from the running application; only the
<code>/api/v1</code> resources have stable, documented response schemas.</p>

<h2 id="authentication">Authentication</h2>
<p>Every public endpoint requires an authenticated session or API key, except
the documentation page itself. Unauthenticated requests receive
<code>401</code> with <code>{{"code": "unauthorized"}}</code>. Requests are
authorized with the same ownership and workspace-role checks as the web UI.</p>

<h2 id="pagination">Pagination</h2>
<p>List endpoints accept <code>page</code> (1-based) and <code>limit</code>
(default 50, maximum 100) and return:</p>
<pre><code>{{"items": [], "total": 0, "page": 1, "limit": 50, "next_cursor": null}}</code></pre>
<p>Oversized limits are clamped; malformed values return a structured
<code>400 invalid_input</code> error. See <code>docs/pagination.md</code> for the
full contract.</p>

<h2 id="errors">Errors</h2>
<p>Errors use one stable JSON shape:</p>
<pre><code>{{"error": "The request is invalid.", "kind": "invalid",
"code": "invalid_input"}}</code></pre>
<p>Treat <code>code</code> as the contract. The catalog lives in
<code>app/api/error_catalog.py</code>; <code>docs/api-errors.md</code> lists every
code and HTTP status. Internal exception text is never exposed.</p>

<h2 id="webhook-verification">Webhook verification</h2>
<p>Webhook deliveries are signed over the exact raw bytes sent:
<code>&lt;timestamp&gt;.&lt;body&gt;</code>, using HMAC-SHA256 with the
subscription secret. Verify the <code>X-AICA-Signature: v1=&lt;hex&gt;</code>
header against <code>X-AICA-Timestamp</code> with a constant-time comparison
and reject timestamps outside the 300-second tolerance window. Verify the raw
body before parsing or re-encoding JSON. See <code>docs/webhooks.md</code>.</p>

<h2 id="endpoints">Endpoints</h2>
<table>
<thead><tr><th>Method</th><th>Path</th><th>Description</th></tr></thead>
<tbody>
{endpoint_rows}
</tbody>
</table>

<div class="note">
<h2 id="coverage">Coverage and stability</h2>
<p>The <code>/api/v1</code> paths are stable and versioned. Other listed paths
keep their existing contracts and may change with the web UI. Automatic route
discovery guarantees the inventory is current; it does not imply that every
path has a frozen schema.</p>
</div>
</main>
</body>
</html>
"""


def main() -> None:
    app = create_app("testing")
    spec = build_spec(app)
    SPEC_PATH.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(render_html(spec), encoding="utf-8")
    print(f"Wrote {SPEC_PATH.relative_to(REPO_ROOT)} and {HTML_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
