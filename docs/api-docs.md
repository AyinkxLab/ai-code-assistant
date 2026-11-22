# Public API documentation site

The published reference is [api/index.html](api/index.html), generated from
[openapi.json](openapi.json) by `scripts/render_api_docs.py`. The running app
serves it at `/api/docs`, and the README and app footer link to it.

## Regenerating

```bash
python scripts/render_api_docs.py
```

The generator has no third-party dependencies. It boots the application with
the testing configuration (no database access) and builds the OpenAPI document
from Flask's registered URL map, then renders one self-contained HTML page
with inline CSS. Re-run it whenever a public route changes.

## Coverage

- Paths are discovered automatically, so the endpoint inventory cannot drift
  from the code.
- Request/response schemas are frozen only for `/api/v1` resources
  ([v1 reference](api-v1.md)). Other
  listed paths (for example `/api/conversations`) keep their existing
  contracts and may change with the web UI; the reference says so.
- Module-private APIs under `/reviews/api`, `/workspaces/api` and similar
  prefixes are intentionally not part of the public reference.
- Authentication, pagination, errors, and webhook verification are documented
  on the generated page and link to their full guides:
  [API errors](api-errors.md), [pagination](pagination.md),
  [webhooks](webhooks.md).
