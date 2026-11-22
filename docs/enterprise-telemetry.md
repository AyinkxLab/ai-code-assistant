# Enterprise adoption telemetry

Phase 12 (#645) adds bounded, privacy-safe counters for enterprise feature
adoption. They answer "is this capability being used?" without recording who
used it or what they configured.

The implementation is `app/observability/enterprise.py`; the Grafana dashboard
is `deploy/monitoring/enterprise-adoption-dashboard.json`.

## Metrics

| Metric | Type | Labels | Meaning |
| ------ | ---- | ------ | ------- |
| `enterprise_feature_events_total` | counter | `feature`, `action`, `outcome` | Every adoption event with the canonical feature label. |
| `enterprise_sso_events_total` | counter | `action`, `outcome` | Domain verification and SSO enforcement events. |
| `enterprise_scim_events_total` | counter | `action`, `outcome` | SCIM provisioning events. |
| `enterprise_custom_role_events_total` | counter | `action`, `outcome` | Custom role create/assign/revoke events. |
| `enterprise_audit_export_events_total` | counter | `format`, `outcome` | Audit export downloads by bounded format. |

### Fixed label values

Only these values are emitted. Any other value collapses to `other`, so a
caller cannot grow the series set with an id, email, domain, or token.

- `feature`: `sso`, `scim`, `custom_roles`, `audit_export`, `other`
- `action`: `challenge`, `verify`, `enforce`, `disable`, `provision`,
  `deprovision`, `assign`, `revoke`, `export`, `create`, `update`, `delete`,
  `use`, `login`, `logout`, `sync`, `other`
- `outcome`: `success`, `denied`, `invalid`, `error`, `other`
- `format`: `json`, `csv`, `other`

The shared registry caps every family at `MAX_SERIES_PER_METRIC` and folds
overflow into an `other` series as a second line of defence.

## What is never recorded

- User ids, usernames, email addresses, workspace/tenant names, domain names.
- SSO/SCIM configuration, client secrets, tokens, or request bodies.
- Any free-form error text. Only the bounded outcome enum is stored.

## How events are emitted

`app/observability/__init__.py` calls `record_from_request` from the shared
`after_request` hook. The mapping is deliberately small:

| Route | Feature / action |
| ----- | ---------------- |
| `POST /auth/api/domains/challenge` | `sso` / `challenge` |
| `POST /auth/api/domains/verify` | `sso` / `verify` |
| `PATCH /auth/api/domains/<id>` | `sso` / `enforce` |
| `GET /admin/audit/export` | `audit_export` / `export` |

Success, invalid input, permission denial, and server errors are all counted
from the final HTTP status. SCIM and custom-role integrations use
`record_scim_event` and `record_custom_role_event` when those endpoints land;
the counters and bounded labels already exist so their dashboards do not need a
later schema change.

## Dashboard

`deploy/monitoring/enterprise-adoption-dashboard.json` contains:

- adoption totals per feature;
- SSO events split by action and outcome;
- audit export volume by format and outcome;
- a 30-day trend using `increase(...[30d])`.

Import it into Grafana and select the Prometheus data source that scrapes
`/metrics`.

## Tests

`tests/test_enterprise_telemetry.py` asserts the metric and label shapes, the
`other` overflow for unknown values, and that identifiers, domains, and secrets
never appear in the rendered exposition.
