# Curated plugin collections (#694)

Collections are maintainer-curated groups of marketplace plugin ids for
onboarding and security. They are **metadata only**: a collection never installs
or executes a plugin, never downloads an artifact and never grants a capability.
Installing from a collection still goes through the normal signed, allowlisted,
workspace-scoped install flow described in [`plugins.md`](plugins.md).

Maintainers are site admins (`User.is_admin`). The backend enforces that on every
write route; the UI is never trusted.

## Data model

`PluginCollection` (`app/models/plugin_collection.py`):

| Column | Bound |
| --- | --- |
| `slug` | 64 chars, unique, `^[a-z0-9]+(-[a-z0-9]+)*$`; derived from `name` when omitted |
| `name` | 120 chars, required |
| `description` | 280 chars, required (may be empty), stored as a plain string |
| `featured` | boolean |
| `plugin_ids` | at most 50 unique ids; each up to 64 chars matching `[A-Za-z0-9._-]+` |
| `created_by_id` | the creating admin, for attribution |

Plugin ids do not have to be registered locally: the catalog may advertise a
plugin before it is installed, and a typo is surfaced as "not available in this
deployment" rather than silently dropped. Validation, error codes and
normalization live in `app/services/plugin_collections.py` and are shared by the
HTML forms and the JSON API.

## Pages

| Route | Access | Purpose |
| --- | --- | --- |
| `GET /plugins/collections` | signed-in user | Browse featured and curated collections with resolved plugin names. |
| `GET /plugins/collections/manage` | site admin | Curation UI (create, feature/unfeature, delete). Non-admins get the 403 page. |
| `POST /plugins/collections/manage` | site admin | Create from the form. Invalid input re-renders with a 400 and the reason. |
| `POST /plugins/collections/<id>/feature` | site admin | Toggle the featured flag. |
| `POST /plugins/collections/<id>/delete` | site admin | Delete a collection. |

Forms carry the standard CSRF token. Unknown or malformed ids return the same
bounded errors as the API; the 403 page never reveals whether a collection
exists.

## JSON API

| Route | Access | Notes |
| --- | --- | --- |
| `GET /plugins/api/collections` | signed-in user | `?featured=true` narrows to featured collections. Featured items sort first, then name. |
| `GET /plugins/api/collections/<slug-or-id>` | signed-in user | Returns `404` + `{"code":"not_found"}` when absent. |
| `POST /plugins/api/collections` | site admin | Body: `name`, optional `slug`, `description`, `featured`, `plugin_ids`. `201` on success. |
| `PUT/PATCH /plugins/api/collections/<id>` | site admin | Partial update of the same fields. |
| `DELETE /plugins/api/collections/<id>` | site admin | `204` on success. |

The marketplace payload (`GET /plugins/api/marketplace`) includes a
`featured_collections` array so catalogue clients can render onboarding groups
without a second request.

Structured errors use `{"error": "<reason>", "code": "<code>"}`:

| Status | Code | When |
| --- | --- | --- |
| `400` | `invalid` | Missing/oversized fields, bad slug, bad plugin id, duplicate id, >50 ids, non-boolean `featured`. |
| `403` | `forbidden` | Authenticated caller is not a site admin. |
| `404` | `not_found` | No collection with that id/slug. |
| `409` | `conflict` | Another collection already uses the slug. |

Example:

```bash
curl -X POST http://127.0.0.1:5000/plugins/api/collections \
  -H 'Content-Type: application/json' \
  -b session=<admin-session> \
  -d '{
        "name": "Security review starter",
        "description": "Audit-friendly plugins for a new workspace.",
        "featured": true,
        "plugin_ids": ["security-scan", "dependency-audit"]
      }'
```

## Implementation and tests

- Model: `app/models/plugin_collection.py`; migration
  `migrations/versions/d2e3f4a5b6c7_add_plugin_collections.py`.
- Service: `app/services/plugin_collections.py`.
- Routes/templates: `app/plugins/collections.py`,
  `app/templates/plugins/collections.html`,
  `app/templates/plugins/collections_manage.html`.
- Tests: `tests/test_plugin_collections.py` covers admin-only access, structured
  validation (description, slug, plugin count/format, featured, duplicate slug),
  lifecycle, public listing/featured filter and page rendering.

The collection migration also merges the background-job and
GitHub-connection-status migration branches into one head. The previously
duplicated revision id (`c9e1f2a3b4d5`) was assigned a unique id
(`c9e1f2a3b4e6`) so `flask db upgrade` reaches a single schema.
