# Security ticketing integration (Jira and Linear)

When a high-risk review finding is acknowledged (`addressed=true`), the app can
create one Jira or Linear issue for it. The integration is opt-in per workspace
and is implemented in
[`app/services/security_tickets.py`](../app/services/security_tickets.py), wired
to `PATCH /reviews/api/reviews/findings/<id>`.

"High risk" means severity `critical` or `high` — the same set used by the
quality dashboard (`app/services/metrics.py`).

## Configuration

Only a workspace owner (`manage_integrations`) may configure it. Credentials are
validated, encrypted with AES-256-GCM, and stored by
[`integration_credentials`](integration-credentials.md) under
`integration="ticketing"`. The plaintext is never returned or logged.

```python
from app.services.security_tickets import configure_ticketing

configure_ticketing(
    workspace_id,
    owner,
    provider="jira",
    base_url="https://acme.atlassian.net",
    email="security@acme.test",
    api_token="...",
    project_key="SEC",
)

# Or Linear:
configure_ticketing(
    workspace_id,
    owner,
    provider="linear",
    api_token="...",
    team_id="<linear-team-id>",
)
```

| Provider | Required credentials |
| --- | --- |
| `jira` | `base_url` (http/https, no embedded credentials), `email`, `api_token`, `project_key` |
| `linear` | `api_token`, `team_id` |

`api_token`, `email`, and provider settings are stored encrypted; `provider` is
stored as a credential too so the workspace can switch providers without a
schema change. `is_configured(workspace_id)` returns `True` only when the full
credential set for the stored provider exists.

The outbound request timeout uses the existing
`INTEGRATION_REQUEST_TIMEOUT` setting (default 10 seconds). No new environment
variables are required.

## What happens on acknowledgement

1. The existing finding update route validates ownership and applies
   `addressed`.
2. If the new state is `true` and the severity is high risk, the service checks
   the workspace configuration. Not configured, no workspace (for example a PR
   review), not high risk, or not acknowledged are **no-ops**.
3. The service builds a bounded payload and posts it to Jira
   (`POST /rest/api/3/issue`) or Linear (`https://api.linear.app/graphql`).
4. On success it stores a `security_ticket_links` row and returns it in the
   response as `"ticket": {...}`.

### Idempotency

`security_ticket_links` has a unique `(workspace_id, finding_id)` constraint.
The service looks the row up before calling the provider, so a retried request —
including a client retry after a successful response was lost — returns the
stored link without a second issue. `(provider, external_key)` is also unique.
The description includes a deterministic idempotency key derived from
`sha256("ticketing:<workspace_id>:<finding_id>")` to help a human find the
intended duplicate. If two workers race, the loser reads the stored row after a
commit failure instead of creating a second link.

### Bounded payload

Only known finding fields are copied: severity, category, file path and line,
explanation, recommendation, confidence, finding id, and review id. Titles are
capped at 200 characters and descriptions at 4,000; the encoded JSON request is
capped at 16 KiB. The integration does not read repository files, metadata
blobs, credentials, or raw request bodies.

## Errors

Service errors carry a stable `code` and HTTP status:

| Exception | `code` | HTTP |
| --- | --- | --- |
| `TicketingValidationError` | `invalid_input` | 400 |
| `TicketingPermissionError` | `permission_denied` | 403 |
| `TicketingNotConfigured` | `not_configured` | 409 |
| `TicketingNotFound` | `not_found` | 404 |
| `TicketingDeliveryError` | `delivery_failed` | 502 |

The acknowledgement route maps them to the shared
[error catalog](api-errors.md), for example `502 bad_gateway` for a provider
failure. A delivery failure also rolls back the acknowledgement, so the finding
is not left marked addressed without its ticket; the client can retry, and the
retry is idempotent once a link exists. Provider error text is sanitized and
truncated (URLs redacted) before it reaches the response.

## Migration

`migrations/versions/a3b4c5d6e7f8_add_security_ticket_links.py` creates
`security_ticket_links`. Apply it with `flask --app wsgi db upgrade`. Deleting a
workspace or a finding cascades to its ticket links.

## Tests

[`tests/test_security_tickets.py`](../tests/test_security_tickets.py) uses a
mocked `post` callable, so no network access or real provider is needed. It
covers encrypted credential storage, owner-only configuration, invalid settings,
Jira and Linear payloads, bounded descriptions, idempotent retries, missed
triggers, delivery failures, and the acknowledgement route (including rollback).
