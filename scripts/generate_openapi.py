"""Generate and validate the public OpenAPI 3.1 document (issue #652).

The document is derived from Flask's routing table so it cannot silently omit a
public endpoint. CI generates it, validates the structure, compares it with the
committed file when one exists (``--check``), and uploads it as an artifact.

Usage::

    python scripts/generate_openapi.py --output openapi.json
    python scripts/generate_openapi.py --output openapi.json --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "openapi.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
# Only paths that expose the public API are documented. HTML pages are out of
# scope for the API contract.
PUBLIC_PATH_MARKER = "/api/"

_PATH_PARAM = re.compile(r"<(?:(?:[^:<>]+):)?([^<>]+)>")


def openapi_path(rule: str) -> str:
    """Convert ``<int:workspace_id>`` segments to ``{workspace_id}``.

    Flask exposes some resources with and without a trailing slash. OpenAPI
    treats those as the same public path, so normalize to a single form to keep
    operation ids unique.
    """
    return _PATH_PARAM.sub(r"{\1}", rule).rstrip("/") or "/"


def _parameters(rule: str) -> list[dict]:
    parameters = []
    for name in _PATH_PARAM.findall(rule):
        is_int = f"<int:{name}>" in rule
        parameters.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "integer" if is_int else "string"},
            }
        )
    if not parameters:
        return []
    return parameters


def _operation_id(method: str, endpoint: str, path: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", f"{method}_{endpoint}_{path}").strip("_")
    return slug.lower()


def build_document(app) -> dict:
    """Return a deterministic OpenAPI 3.1 document for the app's public API."""
    paths: dict[str, dict] = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda item: item.rule):
        if rule.endpoint == "static" or PUBLIC_PATH_MARKER not in rule.rule:
            continue
        path = openapi_path(rule.rule)
        parameters = _parameters(rule.rule)
        methods = {method.lower() for method in rule.methods if method.lower() in HTTP_METHODS}
        for method in sorted(methods):
            operation = {
                "operationId": _operation_id(method, rule.endpoint, path),
                "tags": [path.strip("/").split("/")[0] or "api"],
                "parameters": parameters,
                "responses": {
                    "200": {"description": "Success."},
                    "400": {"$ref": "#/components/responses/BadRequest"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "403": {"$ref": "#/components/responses/Forbidden"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                    "429": {"$ref": "#/components/responses/RateLimited"},
                },
            }
            paths.setdefault(path, {})[method] = operation

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "AI Code Assistant API",
            "version": "0.1.0",
            "description": (
                "Public JSON API. Errors use the stable catalog in "
                "docs/api-errors.md and quota responses include X-RateLimit-* "
                "headers (docs/api-rate-limits.md)."
            ),
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {
            "responses": {
                "BadRequest": {"description": "Invalid input (code invalid_input)."},
                "Unauthorized": {"description": "Authentication required (code unauthorized)."},
                "Forbidden": {"description": "Denied by policy (code forbidden)."},
                "NotFound": {"description": "Unknown resource (code not_found)."},
                "RateLimited": {"description": "Quota exceeded (code rate_limited)."},
            }
        },
    }


def validate_document(document: dict) -> list[str]:
    """Return structural problems with the generated document."""
    errors: list[str] = []
    if document.get("openapi") != "3.1.0":
        errors.append("openapi must be 3.1.0")

    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        errors.append("paths must be a non-empty object")
        return errors

    seen_operation_ids: set[str] = set()
    for path, operations in paths.items():
        if not path.startswith("/"):
            errors.append(f"path {path!r} must start with /")
        if not isinstance(operations, dict) or not operations:
            errors.append(f"path {path!r} has no operations")
            continue
        for method, operation in operations.items():
            if method not in HTTP_METHODS:
                errors.append(f"{path}: unsupported method {method!r}")
                continue
            operation_id = operation.get("operationId")
            if not operation_id:
                errors.append(f"{method.upper()} {path}: missing operationId")
            elif operation_id in seen_operation_ids:
                errors.append(f"{method.upper()} {path}: duplicate operationId {operation_id!r}")
            else:
                seen_operation_ids.add(operation_id)
            responses = operation.get("responses")
            if not isinstance(responses, dict) or not responses:
                errors.append(f"{method.upper()} {path}: missing responses")
                continue
            for status in responses:
                if not str(status).isdigit():
                    errors.append(f"{method.upper()} {path}: invalid status {status!r}")
    return errors


def render(app) -> str:
    """Render the document as stable, sorted JSON with a trailing newline."""
    return json.dumps(build_document(app), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the output file differs from the generated document.",
    )
    args = parser.parse_args(argv)

    from app import create_app

    text = render(create_app("testing"))
    errors = validate_document(json.loads(text))
    if errors:
        print("OpenAPI document is invalid:")
        for error in errors:
            print(f"  - {error}")
        return 1

    if args.check:
        if not args.output.exists():
            print(f"OpenAPI document not found: {args.output}")
            return 1
        if args.output.read_text(encoding="utf-8") != text:
            print(f"OpenAPI document is out of date: {args.output}")
            print("Regenerate it with: python scripts/generate_openapi.py")
            return 1
        print(f"OpenAPI document is valid and up to date: {args.output}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {args.output} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
