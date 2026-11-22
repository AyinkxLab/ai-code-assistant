# API versioning and deprecation

Stable public resources live under the `/api/v1` path prefix, for example:

- `GET /api/v1/projects`
- `GET /api/v1/projects/<id>`
- `GET /api/v1/reviews`
- `GET /api/v1/reviews/<id>`
- `GET /api/v1/usage`

The path is the primary version selector. Versioning is implemented in
`app/api/versioning.py` and installed during application startup by
`init_versioning(app)`.

## `X-API-Version` response header

Every response whose path is `/api` or starts with `/api/` carries an
`X-API-Version` header, including error responses such as an unauthenticated
`401`. Clients can therefore detect the contract version without parsing the
path.

```text
X-API-Version: 1
```

`SUPPORTED_VERSIONS` currently lists only `"1"`.

## Deprecation headers

Deprecated endpoints announce their status with the standard headers:

| Header | Meaning |
| ------ | ------- |
| `Deprecation` | `true` once the endpoint is deprecated. |
| `Sunset` | HTTP date after which the endpoint may be removed. |
| `Link` | Optional successor reference, formatted as `<url>; rel="successor-version"`. |

Use `apply_deprecation(response, sunset=..., replacement=...)` for an existing
response, or decorate a view with `@deprecated(sunset=..., replacement=...)`.
The decorator preserves existing `Link` values by appending the successor link.

```python
from app.api.versioning import deprecated

@bp.route("/legacy-projects")
@deprecated(sunset="Wed, 01 Jan 2027 00:00:00 GMT", replacement="/api/v2/projects")
def legacy_projects():
    ...
```

## Support window

A version remains supported while it is listed in `SUPPORTED_VERSIONS`.
Deprecated endpoints keep working until their advertised `Sunset` date, and the
project's policy is to announce a deprecation and successor path at least 12
months before sunset so integrations have time to migrate. Clients should treat
an unexpected `X-API-Version`, a `Deprecation: true` header, or a new successor
`Link` as a signal to plan an upgrade.
